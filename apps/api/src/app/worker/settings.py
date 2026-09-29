"""arq worker settings. Run with ``manage worker`` (adds /healthz on $PORT for Cloud Run) or
plain ``arq app.worker.settings.WorkerSettings``.

Cron runs inside the worker (arq de-duplicates cron runs across replicas by job id), so the
Cloud Run worker service can scale to N instances without double-firing schedules (ADR-0003).
"""

from __future__ import annotations

from typing import Any, ClassVar

from arq import cron
from redis.asyncio import Redis

from app.clock import KST
from app.db.session import dispose_engine, get_engine
from app.log import configure_logging
from app.notify.channels import build_channels
from app.observability import init_sentry
from app.runtime import build_runtime
from app.settings import get_settings
from app.worker import tasks
from app.worker.queue import QUEUE, redis_settings

_settings = get_settings()


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging(json=_settings.log_json, level=_settings.log_level, service="app-worker")
    init_sentry(_settings, component="worker")
    get_engine()
    redis = Redis.from_url(_settings.redis_url)
    ctx["runtime"] = build_runtime(_settings, redis=redis)
    ctx["channels"] = build_channels(_settings)


async def shutdown(ctx: dict[str, Any]) -> None:
    runtime = ctx.get("runtime")
    if runtime is not None and runtime.redis is not None:
        await runtime.redis.aclose()
    await dispose_engine()


class WorkerSettings:
    functions: ClassVar[list[Any]] = [
        tasks.ingest_source,
        tasks.process_document,
        tasks.link_signals,
        tasks.refresh_recommendations,
        tasks.enqueue_alerts,
        tasks.deliver_notifications,
        tasks.relink_signal,
        tasks.nightly_backtest,
        tasks.refresh_lifecycle,
        tasks.renew_subscriptions,
        tasks.sweep_pending_documents,
    ]
    cron_jobs: ClassVar[list[Any]] = [
        cron(tasks.ingest_procurement, minute=7, run_at_startup=False),  # hourly
        cron(tasks.ingest_minutes, hour=3, minute=10),
        cron(tasks.ingest_budget_books, weekday="sun", hour=2, minute=40),
        cron(tasks.sweep_pending_documents, minute={0, 10, 20, 30, 40, 50}),
        cron(tasks.deliver_notifications, minute=set(range(60))),
        cron(tasks.daily_digest, hour=8, minute=10),
        cron(tasks.weekly_digest, weekday="mon", hour=8, minute=20),
        cron(tasks.renew_subscriptions, minute=17),
        cron(tasks.refresh_lifecycle, hour=4, minute=0),
        cron(tasks.nightly_backtest, hour=4, minute=30),
    ]
    queue_name = QUEUE
    redis_settings = redis_settings(_settings)
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = _settings.worker_max_jobs
    job_timeout = 900
    max_tries = tasks.MAX_TRIES
    keep_result = 3600
    timezone = KST
    health_check_interval = 30
