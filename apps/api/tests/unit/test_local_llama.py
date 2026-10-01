"""The free provider must stay local and fail closed on invalid model responses."""

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.eval.llm_compare import Candidate, build_provider, estimate, run_candidate
from app.llm.budget import MemorySpendGuard
from app.llm.prompts import ChunkContext
from app.llm.providers.heuristic import HeuristicProvider
from app.llm.providers.local_llama import LOCAL_MODEL_ID, LocalLlamaProvider
from app.llm.service import LLMService, estimate_extract_cost
from app.llm.types import (
    LLMConfigError,
    LLMInvalidOutputError,
    LLMRefusedError,
    LLMSetupError,
    LLMUnavailableError,
)
from app.runtime import build_llm
from app.settings import Settings

CTX = ChunkContext("budget_book", "예산", "별빛시", date(2025, 12, 1), [], "인건비 1,000", 2026)


def response(**changes: Any) -> dict[str, Any]:
    return {
        "id": "local-test",
        "model": "Qwen3-4B-Q4_K_M",
        "choices": [{"finish_reason": "stop", "message": {"content": '{"signals":[]}'}}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 10},
        **changes,
    }


async def test_local_request_is_schema_constrained_and_records_zero_api_cost() -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        assert str(req.url) == "http://127.0.0.1:18080/v1/chat/completions"
        assert "authorization" not in req.headers
        body = json.loads(req.content)
        assert body["response_format"]["json_schema"]["schema"]["$defs"]["ExtractedSignal"]
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        assert body["seed"] == 42
        assert "인건비 1,000" in body["messages"][1]["content"]
        return httpx.Response(200, json=response())

    provider = LocalLlamaProvider(transport=httpx.MockTransport(handle))
    result = await provider.extract(CTX)
    assert result.value.signals == []
    assert result.usage.input_tokens == 200
    assert result.usage.output_tokens == 10
    assert result.usage.cost_usd(result.model) == Decimal(0)
    assert estimate_extract_cost(result.model, "인건비") == Decimal(0)
    await MemorySpendGuard(0).check(result.usage.cost_usd(result.model))


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com",
        "http://localhost:18080",
        "http://127.0.0.1.example.com",
        "http://user:password@127.0.0.1:18080",
        "http://127.0.0.1:18080/?route=paid",
    ],
)
def test_nonlocal_or_ambiguous_endpoints_are_refused(url: str) -> None:
    with pytest.raises(LLMSetupError):
        LocalLlamaProvider(base_url=url)


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": None},
        response(usage=None),
        response(choices=[{"finish_reason": "length", "message": {"content": '{"signals":[]}'}}]),
        response(choices=[{"finish_reason": "stop", "message": {"content": '{"signals":[{}]}'}}]),
    ],
)
async def test_malformed_or_truncated_output_is_not_accepted(payload: dict[str, Any]) -> None:
    provider = LocalLlamaProvider(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    with pytest.raises(LLMInvalidOutputError):
        await provider.extract(CTX)


async def test_wrong_served_model_stops_the_job() -> None:
    provider = LocalLlamaProvider(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=response(model="different"))
        )
    )
    with pytest.raises(LLMSetupError, match="model"):
        await provider.extract(CTX)


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (503, LLMUnavailableError),
        (429, LLMUnavailableError),
        (400, LLMConfigError),
        (302, LLMConfigError),
    ],
)
async def test_http_errors_are_typed_and_redirects_not_followed(
    status: int, error: type[Exception]
) -> None:
    provider = LocalLlamaProvider(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, headers={"location": "https://api.example.com"})
        )
    )
    with pytest.raises(error):
        await provider.extract(CTX)


async def test_connection_failure_is_retryable() -> None:
    def fail(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=req)

    with pytest.raises(LLMUnavailableError):
        await LocalLlamaProvider(transport=httpx.MockTransport(fail)).extract(CTX)


def test_runtime_and_evaluation_select_local_provider_without_paid_credentials() -> None:
    settings = Settings(llm_provider="local_llama", llm_daily_budget_usd=0)
    service = build_llm(settings, MemorySpendGuard(0))
    assert isinstance(service.primary, LocalLlamaProvider)
    candidate = Candidate.parse(LOCAL_MODEL_ID)
    assert isinstance(build_provider(candidate, api_key=None), LocalLlamaProvider)
    assert estimate([], candidate) == Decimal(0)


async def test_free_local_calls_are_not_blocked_by_earlier_paid_spend() -> None:
    guard = MemorySpendGuard(0)
    await guard.record(Decimal("0.01"))
    provider = LocalLlamaProvider(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response()))
    )
    calls: list[Any] = []
    session = cast(AsyncSession, SimpleNamespace(add=calls.append))
    service = LLMService(
        primary=provider, fallback=HeuristicProvider(), guard=guard, use_cache=False
    )
    attempt = await service.extract(session, CTX)
    assert not attempt.degraded
    assert attempt.extractor.startswith("local_llama:")
    assert calls[0].status == "ok"
    assert calls[0].cost_usd == Decimal(0)
    assert await guard.spent_today() == Decimal("0.01")


async def test_refusal_cannot_become_an_empty_success() -> None:
    payload = response(
        choices=[
            {
                "finish_reason": "stop",
                "message": {
                    "content": '{"signals":[]}',
                    "refusal": "cannot comply",
                },
            }
        ]
    )
    provider = LocalLlamaProvider(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    with pytest.raises(LLMRefusedError):
        await provider.extract(CTX)


async def test_comparison_can_run_local_after_paid_budget_is_exhausted() -> None:
    guard = MemorySpendGuard(0)
    await guard.record(Decimal("0.01"))
    provider = LocalLlamaProvider(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response()))
    )
    runs = await run_candidate(
        provider,
        [
            {
                "id": "invented-negative",
                "doc_type": "budget_book",
                "institution": "별빛시",
                "date": "2025-12-01",
                "text": "인건비 1,000",
                "labels": [],
                "expected": [],
            }
        ],
        guard=guard,
        concurrency=1,
    )
    assert runs[0].error is None
    assert runs[0].usage.input_tokens == 200
