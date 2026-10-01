"""Optional CPU extraction through a pinned, loopback-only llama.cpp server.

No credentials, proxies or redirects are used. Run the pinned model using the local
runbook; the HTTP model alias is checked, but is not a cryptographic attestation of weights.
The normal pipeline still applies grounding before storing any returned signal.
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.llm.prompts import (
    EXTRACT_PROMPT_VERSION,
    EXTRACT_SYSTEM,
    BriefFacts,
    ChunkContext,
    extract_user_message,
)
from app.llm.providers.heuristic import HeuristicProvider
from app.llm.schemas import ExtractionOutput, strict_json_schema
from app.llm.types import (
    LOCAL_QWEN_MODEL_ID,
    LLMConfigError,
    LLMInvalidOutputError,
    LLMRefusedError,
    LLMResult,
    LLMSetupError,
    LLMUnavailableError,
    Usage,
)

LOCAL_MODEL_ID = LOCAL_QWEN_MODEL_ID
MODEL_ALIAS = "Qwen3-4B-Q4_K_M"


class _Message(BaseModel):
    content: str | None = None
    refusal: str | None = None
    reasoning_content: str | None = None


class _Choice(BaseModel):
    finish_reason: str
    message: _Message


class _Tokens(BaseModel):
    prompt_tokens: int = Field(ge=0, strict=True)
    completion_tokens: int = Field(ge=0, strict=True)


class _Envelope(BaseModel):
    model: str
    choices: list[_Choice] = Field(min_length=1, max_length=1)
    usage: _Tokens
    id: str | None = None


class LocalLlamaProvider:
    name = "local_llama"
    extract_model = LOCAL_MODEL_ID
    extract_effort: str | None = "local-v1-seed42-no-thinking"
    brief_model = HeuristicProvider.brief_model

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:18080",
        timeout: float = 480.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        try:
            url = urlsplit(base_url)
            _ = url.port
        except ValueError as exc:
            raise LLMSetupError("invalid local_llama URL or port") from exc
        if (
            url.scheme != "http"
            or url.hostname not in {"127.0.0.1", "::1"}
            or url.username is not None
            or url.password is not None
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise LLMSetupError(
                "local_llama requires a literal HTTP loopback URL without credentials or path"
            )
        self._url = base_url.rstrip("/") + "/v1/chat/completions"
        self._timeout = timeout
        self._transport = transport

    async def extract(self, ctx: ChunkContext) -> LLMResult[ExtractionOutput]:
        started = time.perf_counter()
        request = {
            "model": MODEL_ALIAS,
            "messages": [
                {"role": "system", "content": EXTRACT_SYSTEM},
                {"role": "user", "content": extract_user_message(ctx)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "extraction",
                    "strict": True,
                    "schema": strict_json_schema(ExtractionOutput),
                },
            },
            "chat_template_kwargs": {"enable_thinking": False},
            "reasoning_effort": "none",
            "stream": False,
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "min_p": 0,
            "presence_penalty": 1.5,
            "seed": 42,
            "max_tokens": 4096,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = await client.post(self._url, json=request)
        except httpx.TransportError as exc:
            raise LLMUnavailableError("local_llama unavailable or timed out") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise LLMUnavailableError(f"local_llama HTTP {response.status_code}")
        if not response.is_success:
            raise LLMConfigError(f"local_llama HTTP {response.status_code}; redirects are refused")
        try:
            envelope = _Envelope.model_validate_json(response.content)
            if envelope.model != MODEL_ALIAS:
                raise LLMSetupError("local_llama served an unexpected model alias")
            choice = envelope.choices[0]
            if choice.message.refusal:
                raise LLMRefusedError("local_llama refused extraction")
            if choice.finish_reason != "stop" or choice.message.reasoning_content:
                raise LLMInvalidOutputError("local_llama incomplete output or unexpected thinking")
            if choice.message.content is None:
                raise LLMInvalidOutputError("local_llama empty content")
            output = ExtractionOutput.model_validate_json(choice.message.content)
        except ValidationError as exc:
            raise LLMInvalidOutputError(
                "invalid local_llama response or extraction schema"
            ) from exc
        return LLMResult(
            value=output,
            provider=self.name,
            model=self.extract_model,
            prompt_version=EXTRACT_PROMPT_VERSION,
            usage=Usage(
                input_tokens=envelope.usage.prompt_tokens,
                output_tokens=envelope.usage.completion_tokens,
            ),
            latency_ms=int((time.perf_counter() - started) * 1000),
            served_by=envelope.model,
            request_id=envelope.id,
        )

    async def brief(self, facts: BriefFacts) -> LLMResult[str]:
        # This local option evaluates extraction only; keep the established facts-only template.
        return await HeuristicProvider().brief(facts)
