"""The extraction/brief service: cache → budget guard → primary model → fallback.

Every attempt — hit, miss, refusal, budget skip — is written to ``llm_calls`` so the admin
console can show cost per task/model/prompt version, cache hit rate and degraded-mode rate.
A setup error (bad key, no credit, unknown model) is the exception: extraction stops rather
than degrading, the job's transaction rolls back with its row, and the failure lands in
``job_runs`` instead.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LLMCacheEntry, LLMCall
from app.llm.budget import SpendGuard
from app.llm.prompts import (
    EXTRACT_PROMPT_VERSION,
    EXTRACT_SYSTEM,
    BriefFacts,
    ChunkContext,
    extract_user_message,
)
from app.llm.schemas import EXTRACTION_SCHEMA_VERSION, ExtractionOutput
from app.llm.types import (
    LLMBudgetExceededError,
    LLMConfigError,
    LLMError,
    LLMInvalidOutputError,
    LLMRefusedError,
    LLMResult,
    LLMSetupError,
    LLMUnavailableError,
    Usage,
)
from app.log import get_logger

log = get_logger(__name__)


class Provider(Protocol):
    name: str
    extract_model: str
    extract_effort: str | None
    brief_model: str

    async def extract(self, ctx: ChunkContext) -> LLMResult[ExtractionOutput]: ...
    async def brief(self, facts: BriefFacts) -> LLMResult[str]: ...


@dataclass(slots=True)
class ExtractionAttempt:
    output: ExtractionOutput
    extractor: str  # e.g. "anthropic:claude-opus-5:extract-v3" or "heuristic-v2"
    degraded: bool
    reason: str | None = None


def estimate_extract_cost(model: str, user_message: str, *, output_tokens: int = 900) -> Decimal:
    # Conservative: ~1 token per hangul character, cached system prompt at read price.
    usage = Usage(
        input_tokens=len(user_message),
        output_tokens=output_tokens,
        cache_read_tokens=len(EXTRACT_SYSTEM),
    )
    return usage.cost_usd(model)


def cache_key(model: str, effort: str | None, user_message: str) -> str:
    # Effort changes the answer as much as the model does; without it in the key, raising
    # APP_LLM_EXTRACT_EFFORT would keep serving answers produced at the old effort.
    h = hashlib.sha256()
    parts = (model, effort or "", EXTRACT_PROMPT_VERSION, EXTRACTION_SCHEMA_VERSION, user_message)
    for part in parts:
        h.update(part.encode())
        h.update(b"\x00")
    return h.hexdigest()


class LLMService:
    def __init__(
        self,
        *,
        primary: Provider | None,
        fallback: Provider,
        guard: SpendGuard,
        use_cache: bool = True,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.guard = guard
        self.use_cache = use_cache

    async def _record(
        self,
        session: AsyncSession,
        *,
        task: str,
        provider: str,
        model: str,
        prompt_version: str,
        status: str,
        document_id: int | None,
        result: LLMResult[Any] | None = None,
        error: str | None = None,
    ) -> None:
        usage = result.usage if result else Usage()
        session.add(
            LLMCall(
                task=task,
                provider=provider,
                model=model,
                prompt_version=prompt_version,
                document_id=document_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cost_usd=usage.cost_usd(model) if provider != "heuristic" else Decimal(0),
                latency_ms=result.latency_ms if result else 0,
                status=status,
                served_by=result.served_by if result else None,
                request_id=result.request_id if result else None,
                error=error[:2000] if error else None,
            )
        )

    async def _fallback_extract(
        self, session: AsyncSession, ctx: ChunkContext, document_id: int | None, reason: str
    ) -> ExtractionAttempt:
        result = await self.fallback.extract(ctx)
        return ExtractionAttempt(result.value, result.model, degraded=True, reason=reason)

    async def extract(
        self,
        session: AsyncSession,
        ctx: ChunkContext,
        *,
        document_id: int | None = None,
        final_attempt: bool = True,
    ) -> ExtractionAttempt:
        """Extract signals from one chunk.

        ``final_attempt=False`` lets transient provider failures propagate so the job runner
        retries with backoff; on the final attempt we degrade to the heuristic instead.
        """
        if self.primary is None:
            result = await self.fallback.extract(ctx)
            return ExtractionAttempt(result.value, result.model, degraded=False)

        primary = self.primary
        model = primary.extract_model
        user_message = extract_user_message(ctx)
        key = cache_key(model, primary.extract_effort, user_message)
        extractor_id = f"{primary.name}:{model}:{EXTRACT_PROMPT_VERSION}"

        if self.use_cache:
            cached = await session.get(LLMCacheEntry, key)
            if cached is not None:
                await session.execute(
                    update(LLMCacheEntry)
                    .where(LLMCacheEntry.key == key)
                    .values(hits=LLMCacheEntry.hits + 1)
                )
                await self._record(
                    session,
                    task="extract",
                    provider=primary.name,
                    model=model,
                    prompt_version=EXTRACT_PROMPT_VERSION,
                    status="cache_hit",
                    document_id=document_id,
                )
                return ExtractionAttempt(
                    ExtractionOutput.model_validate(cached.response), extractor_id, degraded=False
                )

        try:
            await self.guard.check(estimate_extract_cost(model, user_message))
            result = await primary.extract(ctx)
        except LLMBudgetExceededError as exc:
            await self._record(
                session,
                task="extract",
                provider=primary.name,
                model=model,
                prompt_version=EXTRACT_PROMPT_VERSION,
                status="budget_skip",
                document_id=document_id,
                error=str(exc),
            )
            return await self._fallback_extract(session, ctx, document_id, "budget_exceeded")
        except LLMUnavailableError as exc:
            await self._record(
                session,
                task="extract",
                provider=primary.name,
                model=model,
                prompt_version=EXTRACT_PROMPT_VERSION,
                status="error",
                document_id=document_id,
                error=str(exc),
            )
            if not final_attempt:
                raise
            return await self._fallback_extract(session, ctx, document_id, "provider_unavailable")
        except LLMSetupError as exc:
            # Every other chunk would fail the same way; see LLMSetupError.
            log.error("llm.extract.stopped", error=str(exc))
            raise
        except (LLMRefusedError, LLMInvalidOutputError, LLMConfigError) as exc:
            status = (
                "refusal"
                if isinstance(exc, LLMRefusedError)
                else ("invalid_output" if isinstance(exc, LLMInvalidOutputError) else "error")
            )
            await self._record(
                session,
                task="extract",
                provider=primary.name,
                model=model,
                prompt_version=EXTRACT_PROMPT_VERSION,
                status=status,
                document_id=document_id,
                error=str(exc),
            )
            log.warning("llm.extract.degraded", reason=status, error=str(exc))
            return await self._fallback_extract(session, ctx, document_id, status)

        cost = result.usage.cost_usd(model)
        await self.guard.record(cost)
        await self._record(
            session,
            task="extract",
            provider=primary.name,
            model=model,
            prompt_version=EXTRACT_PROMPT_VERSION,
            status="ok",
            document_id=document_id,
            result=result,
        )
        if self.use_cache:
            await session.execute(
                insert(LLMCacheEntry)
                .values(
                    key=key,
                    task="extract",
                    model=model,
                    prompt_version=EXTRACT_PROMPT_VERSION,
                    response=result.value.model_dump(mode="json"),
                    hits=0,
                )
                .on_conflict_do_nothing(index_elements=["key"])
            )
        return ExtractionAttempt(result.value, extractor_id, degraded=False)

    async def brief(
        self, session: AsyncSession, facts: BriefFacts, *, document_id: int | None = None
    ) -> tuple[str, str]:
        """Returns (markdown, model). Falls back to the template brief on any LLM failure."""
        if self.primary is not None:
            try:
                await self.guard.check(Decimal("0.05"))
                result = await self.primary.brief(facts)
                await self.guard.record(result.usage.cost_usd(result.model))
                await self._record(
                    session,
                    task="brief",
                    provider=self.primary.name,
                    model=result.model,
                    prompt_version=result.prompt_version,
                    status="ok",
                    document_id=document_id,
                    result=result,
                )
                return result.value, result.model
            except LLMError as exc:
                await self._record(
                    session,
                    task="brief",
                    provider=self.primary.name,
                    model=self.primary.brief_model,
                    prompt_version="brief",
                    status="error",
                    document_id=document_id,
                    error=str(exc),
                )
                log.warning("llm.brief.degraded", error=str(exc))
        result_fb = await self.fallback.brief(facts)
        return result_fb.value, result_fb.model
