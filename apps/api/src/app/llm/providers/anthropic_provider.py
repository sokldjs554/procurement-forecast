"""Claude via the official Anthropic Python SDK.

Request shape, and why:

* ``output_config.format`` (structured outputs) with a strict JSON schema — the response is
  guaranteed to parse; Pydantic then re-applies the numeric constraints the API does not.
* ``output_config.effort`` — the primary cost lever. Bulk extraction runs at ``low`` effort on
  the most capable model rather than on a cheaper model: one model means one prompt-cache
  namespace and one prompt to evaluate (see ADR-0006).
* ``system`` with ``cache_control`` — the frozen extraction prompt (~1.4k tokens) is cached, so
  the per-chunk cost is dominated by the chunk itself.
* Server-side refusal fallback (``fallbacks="default"``) — a classifier false positive on, say,
  a CCTV procurement discussion is re-run on Anthropic's recommended fallback model inside the
  same call instead of silently dropping the document.
* Timeouts and transient errors are retried by the SDK (``max_retries``); what still fails is
  mapped to :class:`LLMUnavailableError` so the *job* backs off and retries later.
"""

from __future__ import annotations

import json
import time
from typing import Any

import anthropic
from anthropic import AsyncAnthropic
from pydantic import ValidationError

from app.llm.prompts import (
    BRIEF_PROMPT_VERSION,
    BRIEF_SYSTEM,
    EXTRACT_PROMPT_VERSION,
    EXTRACT_SYSTEM,
    BriefFacts,
    ChunkContext,
    extract_user_message,
)
from app.llm.schemas import ExtractionOutput, strict_json_schema
from app.llm.types import (
    LLMConfigError,
    LLMInvalidOutputError,
    LLMRefusedError,
    LLMResult,
    LLMSetupError,
    LLMUnavailableError,
    Usage,
)

_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_EXTRACTION_SCHEMA = strict_json_schema(ExtractionOutput)


def _out_of_credit(exc: anthropic.APIStatusError) -> bool:
    """The API turns an empty balance away with a 400 and "Your credit balance is too low to
    access the Anthropic API" (seen on a live run, docs/real-data-minutes-claude.md §6.2)."""
    return "credit balance" in str(exc).lower()


def _usage(resp: Any) -> Usage:
    u = resp.usage
    return Usage(
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )


def _output_config(effort: str | None, fmt: dict[str, Any] | None) -> dict[str, Any]:
    # effort=None leaves the model's default: some models (e.g. Haiku 4.5) take no effort knob.
    config: dict[str, Any] = {}
    if effort is not None:
        config["effort"] = effort
    if fmt is not None:
        config["format"] = fmt
    return config


def _text(resp: Any) -> str:
    return "".join(block.text for block in resp.content if block.type == "text")


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None,
        extract_model: str,
        extract_effort: str | None,
        brief_model: str,
        brief_effort: str | None,
        timeout: float = 90.0,
        max_retries: int = 2,
        prompt_cache: bool = True,
        server_side_fallback: bool = True,
        client: AsyncAnthropic | None = None,
    ) -> None:
        # api_key=None lets the SDK resolve ANTHROPIC_API_KEY / an `ant auth login` profile.
        self._client = client or AsyncAnthropic(
            api_key=api_key, timeout=timeout, max_retries=max_retries
        )
        self.extract_model = extract_model
        self.extract_effort = extract_effort
        self.brief_model = brief_model
        self._brief_effort = brief_effort
        self._prompt_cache = prompt_cache
        self._fallback = server_side_fallback

    def _system(self, text: str) -> list[dict[str, Any]]:
        block: dict[str, Any] = {"type": "text", "text": text}
        if self._prompt_cache:
            block["cache_control"] = {"type": "ephemeral"}
        return [block]

    async def _create(self, **kwargs: Any) -> Any:
        if self._fallback:
            kwargs["betas"] = [_FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        try:
            return await self._client.beta.messages.create(**kwargs)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise LLMSetupError(f"anthropic auth: {exc}") from exc
        except anthropic.NotFoundError as exc:
            raise LLMSetupError(f"anthropic model/endpoint not found: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMUnavailableError(f"anthropic rate limited: {exc}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise LLMUnavailableError(f"anthropic {exc.status_code}: {exc}") from exc
            if _out_of_credit(exc):
                raise LLMSetupError(f"anthropic account out of credit: {exc}") from exc
            raise LLMConfigError(f"anthropic rejected request ({exc.status_code}): {exc}") from exc
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as exc:
            raise LLMUnavailableError(f"anthropic connection: {exc}") from exc
        except TypeError as exc:
            # The SDK only finds out at request time that no key or credentials were configured.
            raise LLMSetupError(f"anthropic client not configured: {exc}") from exc

    async def extract(self, ctx: ChunkContext) -> LLMResult[ExtractionOutput]:
        started = time.perf_counter()
        resp = await self._create(
            model=self.extract_model,
            max_tokens=8000,
            system=self._system(EXTRACT_SYSTEM),
            messages=[{"role": "user", "content": extract_user_message(ctx)}],
            output_config=_output_config(
                self.extract_effort, {"type": "json_schema", "schema": _EXTRACTION_SCHEMA}
            ),
        )
        latency = int((time.perf_counter() - started) * 1000)
        if resp.stop_reason == "refusal":
            raise LLMRefusedError(f"refused: {getattr(resp, 'stop_details', None)}")
        if resp.stop_reason == "max_tokens":
            raise LLMInvalidOutputError("extraction truncated at max_tokens")
        try:
            parsed = ExtractionOutput.model_validate(json.loads(_text(resp)))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise LLMInvalidOutputError(f"invalid extraction output: {exc}") from exc
        return LLMResult(
            value=parsed,
            provider=self.name,
            model=self.extract_model,
            prompt_version=EXTRACT_PROMPT_VERSION,
            usage=_usage(resp),
            latency_ms=latency,
            served_by=getattr(resp, "model", None),
            request_id=getattr(resp, "_request_id", None),
        )

    async def brief(self, facts: BriefFacts) -> LLMResult[str]:
        config = _output_config(self._brief_effort, None)
        started = time.perf_counter()
        resp = await self._create(
            model=self.brief_model,
            max_tokens=6000,
            system=self._system(BRIEF_SYSTEM),
            messages=[{"role": "user", "content": facts.as_prompt()}],
            **({"output_config": config} if config else {}),
        )
        latency = int((time.perf_counter() - started) * 1000)
        if resp.stop_reason == "refusal":
            raise LLMRefusedError("brief refused")
        text = _text(resp).strip()
        if not text:
            raise LLMInvalidOutputError("empty brief")
        return LLMResult(
            value=text,
            provider=self.name,
            model=self.brief_model,
            prompt_version=BRIEF_PROMPT_VERSION,
            usage=_usage(resp),
            latency_ms=latency,
            served_by=getattr(resp, "model", None),
            request_id=getattr(resp, "_request_id", None),
        )
