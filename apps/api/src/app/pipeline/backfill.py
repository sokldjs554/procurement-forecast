"""One-off runs outside the worker: backfill a source over a window, then process what came in.

The worker does the same work one job at a time (``ingest_source`` → ``process_document`` →
``link_signals``). These run it in-process for a first load or a re-run after a fix, and report
what each step cost: calls per operation, retries and errors by kind, rows, seconds. The real
adapters and pipeline stages are used unchanged; only the call budget is added.
"""

from __future__ import annotations

import dataclasses
import time
from collections import Counter
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, IngestRun, Signal, Source
from app.log import get_logger
from app.pipeline.ingest import run_ingest
from app.pipeline.link import link_signals
from app.pipeline.process import pending_document_ids, process_document
from app.runtime import Runtime
from app.sources.base import FetchWindow
from app.sources.clik import ClikMinutesAdapter
from app.sources.crawler import BoardCrawlerAdapter
from app.sources.g2b import G2BAdapter
from app.sources.http import FatalSourceError, TransientSourceError
from app.sources.lofin import LofinBudgetAdapter
from app.sources.registry import build_adapter
from app.sources.resilience import (
    BudgetedLimiter,
    CallBudgetExhaustedError,
    CircuitOpenError,
    QuotaExhaustedError,
)

log = get_logger(__name__)

# A source that fails stops; the others still run (each 조달청 service has its own quota).
SOURCE_ERRORS = (
    TransientSourceError,
    FatalSourceError,
    QuotaExhaustedError,
    CallBudgetExhaustedError,
    CircuitOpenError,
)
# Failures that happen before a request is sent: the provider never saw these attempts.
_NOT_SENT = {"ConnectError", "ConnectTimeout", "PoolTimeout"}


async def resolve_source_keys(session: AsyncSession, names: list[str]) -> list[Source]:
    """``g2b`` (an adapter name) expands to its sources; anything else is a source key."""
    rows = list((await session.scalars(select(Source).order_by(Source.id))).all())
    picked: list[Source] = []
    for name in names:
        matches = [s for s in rows if name in (s.key, s.adapter)]
        if not matches:
            raise LookupError(f"no source {name!r}; run `manage seed` to create the catalog")
        picked += [s for s in matches if s not in picked]
    return picked


async def ingest_window(
    session: AsyncSession,
    runtime: Runtime,
    sources: list[Source],
    window: FetchWindow,
    *,
    rows: int | None = None,
    max_calls: int | None = None,
) -> list[dict[str, Any]]:
    """Ingest each source over ``window`` and commit after each one. Returns a report row per
    source; a source error ends that source (what was stored stays) and is reported."""
    reports: list[dict[str, Any]] = []
    for source in sources:
        limiter = BudgetedLimiter(runtime.limiter, max_calls)
        adapter = build_adapter(source, dataclasses.replace(runtime, limiter=limiter))
        if rows and isinstance(adapter, G2BAdapter):
            adapter.rows = rows
        started = time.perf_counter()
        error: str | None = None
        try:
            await run_ingest(session, source, adapter, runtime, window)
        except SOURCE_ERRORS as exc:
            error = f"{type(exc).__name__}: {exc}"
            log.warning("backfill.source_stopped", source=source.key, error=error)
        await session.commit()
        run = await session.scalar(
            select(IngestRun)
            .where(IngestRun.source_id == source.id)
            .order_by(IngestRun.id.desc())
            .limit(1)
        )
        report: dict[str, Any] = {
            "source": source.key,
            "window": [window.since.isoformat(), window.until.isoformat()],
            "status": run.status if run else "failed",
            "error": error,
            "seconds": round(time.perf_counter() - started, 1),
            # The crawler keys its calls per host (``<source>@<host>``).
            "calls": sum(n for k, n in limiter.calls.items() if k.split("@", 1)[0] == source.key),
        }
        if run is not None:
            report |= {
                "fetched": run.fetched,
                "created": run.created,
                "updated": run.updated,
                "skipped": run.skipped,
            }
        if isinstance(adapter, G2BAdapter):
            report |= _client_report(adapter.client.stats) | {
                "rows_per_call": adapter.rows,
                "operations": {
                    path.rsplit("/", 1)[-1]: dataclasses.asdict(ps)
                    | {"seconds": round(ps.seconds, 1)}
                    for path, ps in adapter.path_stats.items()
                },
            }
        if isinstance(adapter, BoardCrawlerAdapter):
            report["crawl"] = dataclasses.asdict(adapter.stats)
        if isinstance(adapter, ClikMinutesAdapter):
            report |= _client_report(adapter.client.stats)
            report["minutes"] = dict(sorted(adapter.stats.items()))
        if isinstance(adapter, LofinBudgetAdapter):
            report["books"] = dict(sorted(adapter.stats.items()))
            # Page links need the board crawler, and their hosts need network access.
            hosts = Counter(urlsplit(b.url).hostname or "" for b in adapter.page_links)
            report["page_link_hosts"] = dict(hosts.most_common())
        reports.append(report)
    return reports


def _client_report(stats: Counter[str]) -> dict[str, Any]:
    not_sent = sum(v for k, v in stats.items() if k.removeprefix("error:") in _NOT_SENT)
    return {
        "sent": stats["attempts"] - not_sent,
        "retries": stats["retries"],
        "errors": {k[6:]: v for k, v in sorted(stats.items()) if k.startswith("error:")},
    }


async def process_pending(
    session: AsyncSession,
    runtime: Runtime,
    *,
    limit: int | None = None,
    commit_every: int = 200,
    link_slice: int = 2000,
) -> dict[str, Any]:
    """Process every pending document, then link the new signals — the worker's
    ``process_document`` → ``link_signals`` chain, in one process. A document that fails is
    rolled back on its own and counted."""
    started = time.perf_counter()
    signal_ids: list[int] = []
    processed = failed = 0
    failures: Counter[str] = Counter()
    while limit is None or processed + failed < limit:
        batch_size = 500 if limit is None else min(500, limit - processed - failed)
        ids = await pending_document_ids(session, limit=batch_size)
        if not ids:
            break
        for i, doc_id in enumerate(ids, 1):
            try:
                async with session.begin_nested():
                    result = await process_document(session, runtime, doc_id)
            except Exception as exc:  # recorded per document; one bad row must not stop a backfill
                failed += 1
                failures[type(exc).__name__] += 1
                log.warning("backfill.process_failed", document_id=doc_id, error=repr(exc)[:300])
                doc = await session.get(Document, doc_id)
                if doc is not None:
                    doc.parse_status = "failed"
                    doc.parse_error = repr(exc)[:2000]
                continue
            processed += 1
            signal_ids += result.signal_ids
            if i % commit_every == 0:
                await session.commit()
        await session.commit()
        log.info("backfill.process_progress", processed=processed, failed=failed)
    process_seconds = time.perf_counter() - started

    # In slices: one IN list per call, and asyncpg allows 32,767 parameters per statement.
    # Signals were created in publication order, so slicing keeps the linker's chronology.
    link_started = time.perf_counter()
    touched: set[int] = set()
    verdicts: Counter[str] = Counter()
    for at in range(0, len(signal_ids), link_slice):
        ids = signal_ids[at : at + link_slice]
        verdicts.update(
            (await session.scalars(select(Signal.verdict).where(Signal.id.in_(ids)))).all()
        )
        touched.update(await link_signals(session, runtime, ids))
        await session.commit()
    return {
        "documents": processed,
        "failed": failed,
        "failures": dict(failures),
        "signals": len(signal_ids),
        "verdicts": dict(verdicts),
        "opportunities_touched": len(touched),
        "process_seconds": round(process_seconds, 1),
        "link_seconds": round(time.perf_counter() - link_started, 1),
    }
