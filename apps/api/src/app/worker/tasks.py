"""arq job functions.

Pipeline fan-out::

    cron ─► ingest_source(key) ─► process_document(doc) ─► link_signals(ids)
                                                             └► refresh_recommendations(org)
                                                                   └► enqueue_alerts(org) (instant)
    cron ─► deliver_notifications         (every minute)
    cron ─► daily/weekly digests           (08:10 KST)
    cron ─► renew_subscriptions            (hourly)
    cron ─► nightly_backtest / lifecycle   (04:xx KST)

Every job runs inside :func:`tracked`, which records a ``job_runs`` row (for the admin console),
binds ``job``/``job_id`` into the log context, and turns transient failures into arq retries
with exponential backoff. Non-retryable failures are recorded and surface in *Admin → Jobs*.
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, date, datetime, timedelta
from typing import Any, TypeVar, cast

from arq import Retry
from sqlalchemy import select

from app.clock import today_kst
from app.db.models import AlertRule, EvalRun, JobRun, Opportunity, Organization, Signal, Source
from app.db.session import session_scope
from app.llm.types import LLMUnavailableError
from app.log import bind_contextvars, clear_contextvars, get_logger
from app.pipeline.backtest import latest_calibration, run_backtest
from app.pipeline.ingest import default_window, run_ingest
from app.pipeline.link import link_signals as link_signals_impl
from app.pipeline.link import refresh_opportunity
from app.pipeline.process import pending_document_ids
from app.pipeline.process import process_document as process_impl
from app.pipeline.recommend import refresh_recommendations as refresh_recs_impl
from app.runtime import Runtime
from app.sources.http import TransientSourceError
from app.sources.resilience import CircuitOpenError, QuotaExhaustedError
from app.worker.queue import enqueue

log = get_logger(__name__)
MAX_TRIES = 5


def _jsonable(value: Any) -> Any:
    """Keep job args replayable from the admin console (ints, strs, lists of ints)."""
    try:
        json.dumps(value)
        return value
    except TypeError:
        return [str(v) for v in value] if isinstance(value, list) else str(value)


def _runtime(ctx: dict[str, Any]) -> Runtime:
    runtime: Runtime = ctx["runtime"]
    return runtime


F = TypeVar("F", bound=Callable[..., Coroutine[Any, Any, Any]])


def tracked(name: str) -> Callable[[F], F]:
    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
            job_id = str(ctx.get("job_id", "adhoc"))
            attempt = int(ctx.get("job_try", 1))
            clear_contextvars()
            bind_contextvars(job=name, job_id=job_id, attempt=attempt)
            started = time.perf_counter()
            async with session_scope() as s:
                run = JobRun(
                    job=name,
                    job_id=job_id,
                    attempt=attempt,
                    status="running",
                    args={"raw": _jsonable(list(args)), **{k: str(v) for k, v in kwargs.items()}},
                )
                s.add(run)
                await s.flush()
                run_id = run.id
            status, error, result = "succeeded", None, None
            try:
                result = await fn(ctx, *args, **kwargs)
                return result
            except QuotaExhaustedError as exc:
                status, error = "retrying", str(exc)
                raise Retry(
                    defer=max(exc.resets_at - datetime.now(UTC), timedelta(minutes=5))
                ) from exc
            except CircuitOpenError as exc:
                status, error = "retrying", str(exc)
                raise Retry(defer=timedelta(seconds=exc.retry_after + 5)) from exc
            except (TransientSourceError, LLMUnavailableError) as exc:
                if attempt >= MAX_TRIES:
                    status, error = "failed", f"{type(exc).__name__}: {exc}"
                    raise
                status, error = "retrying", str(exc)
                raise Retry(defer=timedelta(seconds=30 * 2 ** (attempt - 1))) from exc
            except asyncio.CancelledError:
                # Worker shutdown (SIGTERM on deploy): arq puts the job back on the queue.
                status, error = "retrying", "cancelled by worker shutdown"
                raise
            except Exception as exc:
                status, error = "failed", f"{type(exc).__name__}: {exc}"
                log.exception("job.failed")
                raise
            finally:
                async with session_scope() as s:
                    run_row = await s.get(JobRun, run_id)
                    if run_row is not None:
                        run_row.status = status
                        run_row.error = error[:4000] if error else None
                        run_row.finished_at = datetime.now(UTC)
                        run_row.duration_ms = int((time.perf_counter() - started) * 1000)
                        if isinstance(result, dict):
                            run_row.result = {
                                k: v
                                for k, v in result.items()
                                if isinstance(v, (int, float, str, bool, list))
                            }
                log.info(
                    "job.finished",
                    status=status,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )

        return cast(F, wrapper)

    return decorator


@tracked("ingest_source")
async def ingest_source(
    ctx: dict[str, Any], source_key: str, since: str | None = None, until: str | None = None
) -> dict[str, Any]:
    from app.sources.base import FetchWindow
    from app.sources.registry import build_adapter

    runtime = _runtime(ctx)
    async with session_scope() as s:
        source = await s.scalar(select(Source).where(Source.key == source_key))
        if source is None or not source.enabled:
            return {"skipped": True}
        window = (
            FetchWindow(
                date.fromisoformat(since), date.fromisoformat(until or today_kst().isoformat())
            )
            if since
            else default_window(source, today_kst())
        )
        stats = await run_ingest(s, source, build_adapter(source, runtime), runtime, window)
    for doc_id in stats.changed_ids:
        await enqueue(
            ctx["redis"],
            "process_document",
            doc_id,
            job_id=f"process:{doc_id}:{int(time.time() // 60)}",
        )
    return {
        "fetched": stats.fetched,
        "created": stats.created,
        "updated": stats.updated,
        "skipped": stats.skipped,
    }


@tracked("process_document")
async def process_document(ctx: dict[str, Any], document_id: int) -> dict[str, Any]:
    runtime = _runtime(ctx)
    final = int(ctx.get("job_try", 1)) >= MAX_TRIES
    async with session_scope() as s:
        result = await process_impl(s, runtime, document_id, final_attempt=final)
    if result.signal_ids:
        await enqueue(
            ctx["redis"],
            "link_signals",
            result.signal_ids,
            job_id=f"link:{document_id}:{result.signal_ids[0]}",
        )
    return {
        "chunks": result.chunks,
        "triaged": result.triaged,
        "signals": len(result.signal_ids),
        "needs_review": result.needs_review,
        "degraded": result.degraded,
    }


@tracked("link_signals")
async def link_signals(ctx: dict[str, Any], signal_ids: list[int]) -> dict[str, Any]:
    runtime = _runtime(ctx)
    async with session_scope() as s:
        calibration = await latest_calibration(s)
        touched = await link_signals_impl(s, runtime, signal_ids, calibration=calibration)
        org_ids = list((await s.scalars(select(Organization.id))).all())
    for org_id in org_ids:
        await enqueue(
            ctx["redis"],
            "refresh_recommendations",
            org_id,
            job_id=f"recs:{org_id}:{int(time.time() // 30)}",
        )
    return {"opportunities": len(touched)}


@tracked("refresh_recommendations")
async def refresh_recommendations(ctx: dict[str, Any], org_id: int) -> dict[str, Any]:
    async with session_scope() as s:
        scored = await refresh_recs_impl(s, org_id)
        rule = await s.get(AlertRule, org_id)
    if rule is not None and rule.mode == "instant":
        await enqueue(
            ctx["redis"],
            "enqueue_alerts",
            org_id,
            "instant",
            job_id=f"alerts:{org_id}:{int(time.time() // 30)}",
        )
    return {"recommendations": len(scored)}


@tracked("enqueue_alerts")
async def enqueue_alerts(ctx: dict[str, Any], org_id: int, mode: str) -> dict[str, Any]:
    from app.notify.dispatch import enqueue_alerts as enqueue_impl

    runtime = _runtime(ctx)
    async with session_scope() as s:
        created = await enqueue_impl(
            s, org_id, web_url=runtime.settings.public_web_url, mode_filter=mode
        )
    return {"notifications": created}


@tracked("deliver_notifications")
async def deliver_notifications(ctx: dict[str, Any]) -> dict[str, Any]:
    from app.notify.dispatch import deliver_pending

    async with session_scope() as s:
        return await deliver_pending(s, ctx["channels"])


async def _digest(ctx: dict[str, Any], mode: str) -> dict[str, Any]:
    async with session_scope() as s:
        org_ids = list(
            (await s.scalars(select(AlertRule.org_id).where(AlertRule.mode == mode))).all()
        )
    for org_id in org_ids:
        await enqueue(
            ctx["redis"],
            "enqueue_alerts",
            org_id,
            mode,
            job_id=f"digest:{mode}:{org_id}:{today_kst():%Y%m%d}",
        )
    return {"orgs": len(org_ids)}


@tracked("daily_digest")
async def daily_digest(ctx: dict[str, Any]) -> dict[str, Any]:
    return await _digest(ctx, "daily")


@tracked("weekly_digest")
async def weekly_digest(ctx: dict[str, Any]) -> dict[str, Any]:
    return await _digest(ctx, "weekly")


@tracked("renew_subscriptions")
async def renew_subscriptions(ctx: dict[str, Any]) -> dict[str, Any]:
    from app.billing.service import build_payment_provider, renew_due

    runtime = _runtime(ctx)
    async with session_scope() as s:
        return await renew_due(s, runtime.settings, build_payment_provider(runtime.settings))


@tracked("sweep_pending_documents")
async def sweep_pending_documents(ctx: dict[str, Any]) -> dict[str, Any]:
    """Safety net: anything left 'pending' (worker crash between commit and enqueue)."""
    async with session_scope() as s:
        ids = await pending_document_ids(s, limit=200)
    for doc_id in ids:
        await enqueue(ctx["redis"], "process_document", doc_id, job_id=f"process:{doc_id}:sweep")
    return {"enqueued": len(ids)}


@tracked("refresh_lifecycle")
async def refresh_lifecycle(ctx: dict[str, Any]) -> dict[str, Any]:
    """Statuses depend on today's date (bid_open → closed, open → dormant)."""
    today = today_kst()
    async with session_scope() as s:
        calibration = await latest_calibration(s)
        opps = (
            await s.scalars(select(Opportunity).where(Opportunity.status.in_(("open", "bid_open"))))
        ).all()
        for opp in opps:
            await refresh_opportunity(s, opp, today=today, calibration=calibration)
    return {"opportunities": len(opps)}


@tracked("nightly_backtest")
async def nightly_backtest(ctx: dict[str, Any]) -> dict[str, Any]:
    async with session_scope() as s:
        metrics = await run_backtest(s)
        s.add(EvalRun(kind="backtest", label="nightly", metrics=metrics))
    return {"tenders": metrics["tender_early_coverage"]["tenders"]}


@tracked("relink_signal")
async def relink_signal(ctx: dict[str, Any], signal_id: int) -> dict[str, Any]:
    """Repair legacy queued review jobs, including rejections and already-linked signals."""
    from app.pipeline.review import reconcile_reviewed_signal

    async with session_scope() as s:
        signal = await s.get(Signal, signal_id, with_for_update=True)
        if signal is None:
            return {"skipped": True}
        touched = await reconcile_reviewed_signal(s, _runtime(ctx), signal)
    return {"opportunities": len(touched)}


async def ingest_cron(ctx: dict[str, Any], keys: tuple[str, ...]) -> None:
    async with session_scope() as s:
        enabled = set(
            (
                await s.scalars(
                    select(Source.key).where(Source.enabled.is_(True), Source.key.in_(keys))
                )
            ).all()
        )
    for key in enabled:
        await enqueue(
            ctx["redis"], "ingest_source", key, job_id=f"ingest:{key}:{datetime.now(UTC):%Y%m%d%H}"
        )


# The sources each ingest cron enqueues (the enabled ones). Every live source belongs to one;
# tests/unit/test_worker_runner.py checks, since a source left out is only ever fetched by hand.
CRON_SOURCES: dict[str, tuple[str, ...]] = {
    "ingest_procurement": ("g2b_order_plan", "g2b_prespec", "g2b_bid"),
    "ingest_minutes": ("clik_minutes", "minutes_boards"),
    # Books come out a few times a year; the boards are crawled incrementally, once a week.
    "ingest_budget_books": ("lofin_budget", "budget_boards"),
}


async def ingest_procurement(ctx: dict[str, Any]) -> None:
    await ingest_cron(ctx, CRON_SOURCES["ingest_procurement"])


async def ingest_minutes(ctx: dict[str, Any]) -> None:
    await ingest_cron(ctx, CRON_SOURCES["ingest_minutes"])


async def ingest_budget_books(ctx: dict[str, Any]) -> None:
    await ingest_cron(ctx, CRON_SOURCES["ingest_budget_books"])
