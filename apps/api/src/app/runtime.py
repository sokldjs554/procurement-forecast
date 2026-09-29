"""Process-wide wiring: builds the long-lived collaborators once per API/worker process.

Keeping construction in one place makes the degraded modes explicit — no API key means the
heuristic extractor, no Voyage key means the hashing embedder, no tesseract binary means OCR is
skipped (and the document is flagged) — and lets tests swap any piece.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field

from redis.asyncio import Redis

from app.domain.embedding import Embedder, HashingEmbedder, VoyageEmbedder
from app.domain.institutions import InstitutionRegistry, load_registry_csv
from app.llm.budget import MemorySpendGuard, RedisSpendGuard, SpendGuard
from app.llm.providers.heuristic import HeuristicProvider
from app.llm.service import LLMService, Provider
from app.log import get_logger
from app.parsing.ocr import OCREngine, TesseractOCR
from app.parsing.ocr_correct import LexiconCorrector, default_lexicon
from app.settings import Settings
from app.sources.resilience import (
    Breaker,
    Limiter,
    MemoryBreaker,
    MemoryLimiter,
    RedisBreaker,
    RedisLimiter,
)

log = get_logger(__name__)


@dataclass(slots=True)
class Runtime:
    settings: Settings
    registry: InstitutionRegistry
    embedder: Embedder
    llm: LLMService
    ocr: OCREngine | None
    corrector: LexiconCorrector
    limiter: Limiter
    breaker: Breaker
    redis: Redis | None = None
    extras: dict[str, object] = field(default_factory=dict)

    @property
    def extractor_mode(self) -> str:
        return "llm" if self.llm.primary is not None else "heuristic"


def build_llm(settings: Settings, guard: SpendGuard) -> LLMService:
    primary: Provider | None = None
    if settings.llm_provider == "anthropic":
        from app.llm.providers.anthropic_provider import AnthropicProvider

        primary = AnthropicProvider(
            api_key=settings.anthropic_api_key.get_secret_value()
            if settings.anthropic_api_key
            else None,
            extract_model=settings.llm_extract_model,
            extract_effort=settings.llm_extract_effort,
            brief_model=settings.llm_brief_model,
            brief_effort=settings.llm_brief_effort,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            prompt_cache=settings.llm_prompt_cache,
            server_side_fallback=settings.llm_server_side_fallback,
        )
    from app.db.session import get_sessionmaker

    return LLMService(
        primary=primary, fallback=HeuristicProvider(), guard=guard, durable=get_sessionmaker
    )


def build_runtime(settings: Settings, redis: Redis | None = None) -> Runtime:
    registry = load_registry_csv()
    embedder: Embedder
    if settings.embedding_provider == "voyage" and settings.voyage_api_key:
        embedder = VoyageEmbedder(
            settings.voyage_api_key.get_secret_value(),
            model=settings.voyage_model,
            dim=settings.embedding_dim,
        )
    else:
        embedder = HashingEmbedder(settings.embedding_dim)
    guard: SpendGuard = (
        RedisSpendGuard(redis, settings.llm_daily_budget_usd)
        if redis is not None
        else MemorySpendGuard(settings.llm_daily_budget_usd)
    )
    ocr: OCREngine | None = None
    if settings.ocr_provider == "tesseract":
        if shutil.which("tesseract"):
            ocr = TesseractOCR(settings.ocr_languages)
        else:
            log.warning("ocr.unavailable", reason="tesseract binary not found")
    lexicon = default_lexicon(
        [inst.name.split()[-1] for inst in registry.all()]
        + [alias for inst in registry.all() for alias in inst.aliases]
    )
    limiter: Limiter
    breaker: Breaker
    if redis is not None:
        limiter = RedisLimiter(
            redis,
            daily_quota={
                "clik_minutes": settings.clik_daily_quota,
                "g2b_order_plan": settings.data_go_kr_daily_quota,
                "g2b_prespec": settings.data_go_kr_daily_quota,
                "g2b_bid": settings.data_go_kr_daily_quota,
            },
        )
        breaker = RedisBreaker(
            redis, settings.circuit_failure_threshold, settings.circuit_cooldown_seconds
        )
    else:
        limiter, breaker = MemoryLimiter(), MemoryBreaker()
    return Runtime(
        settings=settings,
        registry=registry,
        embedder=embedder,
        llm=build_llm(settings, guard),
        ocr=ocr,
        corrector=LexiconCorrector(lexicon),
        limiter=limiter,
        breaker=breaker,
        redis=redis,
    )
