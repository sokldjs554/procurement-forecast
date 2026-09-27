"""Build a source adapter from a ``sources`` row."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from functools import lru_cache
from typing import Any

from app.clock import today_kst
from app.db.models import Source
from app.runtime import Runtime
from app.sources import clik, g2b, lofin
from app.sources.base import DocType, FetchWindow, RawRecord, SourceAdapter
from app.sources.crawler import BoardCrawlerAdapter
from app.sources.http import FatalSourceError, ResilientClient
from app.sources.resilience import Limiter, MemoryLimiter

SOURCE_CATALOG: tuple[dict[str, Any], ...] = (
    {"key": "clik_minutes", "name": "국회도서관 지방의정포털 — 지방의회 회의록", "adapter": "clik"},
    {"key": "lofin_budget", "name": "지방재정365 — 지자체 예산서", "adapter": "lofin"},
    {"key": "g2b_order_plan", "name": "조달청 나라장터 — 발주계획", "adapter": "g2b"},
    {"key": "g2b_prespec", "name": "조달청 나라장터 — 사전규격", "adapter": "g2b"},
    {"key": "g2b_bid", "name": "조달청 나라장터 — 입찰공고", "adapter": "g2b"},
)

FIXTURE_CATALOG: tuple[dict[str, Any], ...] = (
    {"key": "fixture_minutes", "name": "[데모] 합성 지방의회 회의록", "adapter": "fixture"},
    {
        "key": "fixture_budget",
        "name": "[데모] 합성 지자체 누리집 예산 게시판 (크롤링 · PDF·스캔·HWPX)",
        "adapter": "crawler",
    },
    {"key": "fixture_order_plan", "name": "[데모] 합성 발주계획", "adapter": "fixture"},
    {"key": "fixture_prespec", "name": "[데모] 합성 사전규격", "adapter": "fixture"},
    {"key": "fixture_bid", "name": "[데모] 합성 입찰공고", "adapter": "fixture"},
)


@lru_cache(maxsize=4)
def _world(anchor: str, seed: int, scale: float, scanned_ratio: float) -> Any:
    from app.demo.synth import build_world

    return build_world(
        anchor=date.fromisoformat(anchor), seed=seed, scale=scale, scanned_ratio=scanned_ratio
    )


def fixture_world(config: dict[str, Any]) -> Any:
    return _world(
        config.get("anchor", today_kst().isoformat()),
        int(config.get("seed", 7)),
        float(config.get("scale", 1.0)),
        float(config.get("scanned_ratio", 0.3)),
    )


class FixtureAdapter:
    """Serves the deterministic synthetic world as if it were a live provider."""

    def __init__(self, key: str, config: dict[str, Any]) -> None:
        self.key = key
        self._cfg = config
        doc_types: dict[str, DocType] = {
            "fixture_minutes": "council_minutes",
            "fixture_budget": "budget_book",
            "fixture_order_plan": "order_plan",
            "fixture_prespec": "prespec",
            "fixture_bid": "bid_notice",
        }
        self.doc_type: DocType = doc_types[key]

    def world(self) -> Any:
        return fixture_world(self._cfg)

    async def aclose(self) -> None:
        return None

    async def fetch(self, window: FetchWindow) -> AsyncIterator[RawRecord]:
        for rec in self.world().records[self.key]:
            if window.since <= rec.published_at <= window.until:
                yield rec


def build_adapter(source: Source, runtime: Runtime) -> SourceAdapter:
    s = runtime.settings

    def client(base_url: str) -> ResilientClient:
        return ResilientClient(
            source.key,
            base_url=base_url,
            limiter=runtime.limiter,
            breaker=runtime.breaker,
            timeout=s.source_http_timeout_seconds,
            max_attempts=s.source_max_attempts,
        )

    if source.adapter == "fixture":
        return FixtureAdapter(source.key, source.config)
    if source.adapter == "crawler":
        return _crawler(source, runtime)
    if source.adapter == "g2b":
        if not s.data_go_kr_service_key:
            raise FatalSourceError("APP_DATA_GO_KR_SERVICE_KEY is not configured")
        return g2b.G2BAdapter(
            source.key,
            client(g2b.BASE_URL),
            s.data_go_kr_service_key.get_secret_value(),
            rows=int(source.config.get("rows", 100)),
        )
    if source.adapter == "clik":
        if not s.clik_api_key:
            raise FatalSourceError("APP_CLIK_API_KEY is not configured")
        return clik.ClikMinutesAdapter(
            client(clik.BASE_URL),
            s.clik_api_key.get_secret_value(),
            key=source.key,
            council_ids=source.config.get("council_ids"),
            overrides=source.config.get("overrides"),
        )
    if source.adapter == "lofin":
        if not s.lofin_api_key:
            raise FatalSourceError("APP_LOFIN_API_KEY is not configured")
        return lofin.LofinBudgetAdapter(
            client(source.config.get("base_url", lofin.BASE_URL)),
            s.lofin_api_key.get_secret_value(),
            key=source.key,
            institutions=source.config.get("institutions"),
            overrides=source.config.get("overrides"),
        )
    raise FatalSourceError(f"unknown adapter {source.adapter!r}")


def _crawler(source: Source, runtime: Runtime) -> BoardCrawlerAdapter:
    """Real boards come from ``sources.config``; the demo source crawls the synthetic 누리집."""
    s = runtime.settings
    config = dict(source.config)
    transport = None
    limiter: Limiter = runtime.limiter
    if source.key.startswith("fixture_"):
        from app.demo.sites import SyntheticGovSites

        sites = SyntheticGovSites(fixture_world(config).records[source.key])
        config |= {"boards": sites.boards(), "delay_seconds": 0, "structured": {"synthetic": True}}
        transport = sites.transport()
        limiter = MemoryLimiter()  # an in-process fake site needs no politeness

    def client_for_host(host: str) -> ResilientClient:
        return ResilientClient(
            f"{source.key}@{host}",  # rate limit and circuit breaker per host
            base_url=f"https://{host}",
            limiter=limiter,
            breaker=runtime.breaker,
            timeout=s.source_http_timeout_seconds,
            max_attempts=s.source_max_attempts,
            transport=transport,
        )

    return BoardCrawlerAdapter(source.key, config, client_for_host)
