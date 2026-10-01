"""One pass of the worker, for hosts with a database and a scheduler but no resident worker.

The free deployment (docs/free-operations.md) has PostgreSQL and a scheduler that starts a short
process every hour, but no machine that stays up and no Redis. ``manage ops tick`` is that
process. It runs the cron jobs that came due since the previous pass, with the arq worker's own
schedule and job functions (:class:`app.worker.settings.WorkerSettings`), then every follow-up
job they enqueue (process → link → reconcile → recommend → alerts) here, until none is left.

State stays where arq leaves it. Pending documents and unfinished link generations live in
PostgreSQL and the sweeps find them again, so a job that asks to be retried is left for the next
pass instead of being slept on here. Do not run this next to an arq worker on the same database:
the cron schedule would fire twice.
"""

from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from arq import Retry
from arq.cron import CronJob, next_cron
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError

from app.clock import KST, today_kst
from app.db.models import Document, JobRun, Source
from app.db.session import session_scope
from app.log import get_logger
from app.settings import Settings

log = get_logger(__name__)

TICK_JOB = "ops_tick"
# A session-level advisory lock: two overlapping passes would fire the same crons twice.
# scripts/render-runtime.py holds 735912608 while it migrates.
TICK_LOCK = 735912609
# Crons that fetch from providers. The storage guard pauses these and nothing else, so what was
# already collected keeps being processed, linked and recommended.
INGEST_CRONS = frozenset({"ingest_procurement", "ingest_minutes", "ingest_budget_books"})
STORAGE_GUARD_RATIO = 0.9


@dataclass(slots=True)
class QueuedJob:
    function: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    job_id: str | None


class LocalQueue:
    """What the job functions enqueue on (``ctx["redis"]``) during a pass.

    It keeps arq's ``enqueue_job`` contract that matters here: a job id already taken is
    refused, so the deterministic ids the jobs choose still de-duplicate fan-out. Deferral is
    ignored; jobs run in the order they were enqueued."""

    def __init__(self) -> None:
        self.jobs: deque[QueuedJob] = deque()
        self._taken: set[str] = set()
        self._count = 0

    async def enqueue_job(
        self,
        function: str,
        *args: Any,
        _job_id: str | None = None,
        _defer_by: Any = None,
        _queue_name: str | None = None,
        **kwargs: Any,
    ) -> SimpleNamespace | None:
        if _job_id is not None:
            if _job_id in self._taken:
                return None
            self._taken.add(_job_id)
        self._count += 1
        self.jobs.append(QueuedJob(function, args, kwargs, _job_id))
        return SimpleNamespace(job_id=_job_id or f"local:{self._count}")


def job_functions() -> dict[str, Any]:
    from app.worker.settings import WorkerSettings

    table: dict[str, Any] = {}
    for entry in WorkerSettings.functions:
        coroutine = getattr(entry, "coroutine", entry)
        table[getattr(entry, "name", coroutine.__name__)] = coroutine
    return table


def cron_jobs() -> list[CronJob]:
    from app.worker.settings import WorkerSettings

    return list(WorkerSettings.cron_jobs)


def due(jobs: list[CronJob], since: datetime, until: datetime) -> list[CronJob]:
    """The cron jobs that fire in ``(since, until]`` on the worker's clock (KST). A job that
    fired several times in a long gap runs once: every cron job here is a sweep or a fetch
    that resumes from a cursor, so one run catches up."""
    start, end = since.astimezone(KST), until.astimezone(KST)
    return [
        job
        for job in jobs
        if next_cron(
            start,
            month=job.month,
            day=job.day,
            weekday=job.weekday,
            hour=job.hour,
            minute=job.minute,
            second=job.second,
            microsecond=job.microsecond,
        )
        <= end
    ]


@dataclass(slots=True)
class TickReport:
    since: str
    until: str
    crons: list[str] = field(default_factory=list)
    skipped_crons: list[str] = field(default_factory=list)
    jobs: dict[str, int] = field(default_factory=dict)
    succeeded: int = 0
    retry_later: int = 0
    failed: int = 0
    stopped_at_deadline: bool = False
    storage_guard: bool = False
    database_mb: float = 0.0
    pending_documents: int = 0
    pruned_raw_files: int = 0
    locked_out: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def database_megabytes() -> float:
    async with session_scope() as s:
        size = await s.scalar(text("SELECT pg_database_size(current_database())"))
    return round(int(size or 0) / 1_048_576, 1)


async def pending_documents() -> int:
    async with session_scope() as s:
        count = await s.scalar(
            select(func.count()).select_from(Document).where(Document.parse_status == "pending")
        )
    return int(count or 0)


async def last_tick_until() -> datetime | None:
    """Where the previous successful pass stopped looking for due crons."""
    async with session_scope() as s:
        run = await s.scalar(
            select(JobRun)
            .where(JobRun.job == TICK_JOB, JobRun.status == "succeeded")
            .order_by(JobRun.started_at.desc())
            .limit(1)
        )
    if run is None:
        return None
    until = (run.result or {}).get("until")
    return datetime.fromisoformat(until) if isinstance(until, str) else run.started_at


async def _first_window_since(source_key: str, days: int) -> str | None:
    """A source that was never fetched would otherwise backfill the worker's default 30 days;
    a small free database fills up on that alone (조달청: about 57,000 records a month)."""
    async with session_scope() as s:
        cursor = await s.scalar(select(Source.cursor).where(Source.key == source_key))
    if cursor is None or cursor.get("until"):
        return None
    return (today_kst() - timedelta(days=days - 1)).isoformat()


class Pass:
    def __init__(self, ctx: dict[str, Any], report: TickReport, deadline: float) -> None:
        self.ctx = ctx
        self.report = report
        self.deadline = deadline
        self.functions = job_functions()
        self.counts: Counter[str] = Counter()

    @property
    def queue(self) -> LocalQueue:
        queue: LocalQueue = self.ctx["redis"]
        return queue

    def out_of_time(self) -> bool:
        return time.monotonic() > self.deadline

    async def call(
        self, name: str, fn: Any, args: tuple[Any, ...], kwargs: dict[str, Any], job_id: str
    ) -> None:
        self.ctx["job_id"], self.ctx["job_try"] = job_id, 1
        self.counts[name] += 1
        try:
            await fn(self.ctx, *args, **kwargs)
        except Retry:
            # Recorded as 'retrying' by the job itself; its state is durable and the next pass's
            # cron or sweep picks it up again.
            self.report.retry_later += 1
        except Exception:
            self.report.failed += 1
            log.exception("ops.job_failed", job=name)
        else:
            self.report.succeeded += 1

    async def drain(self, first_window_days: int) -> None:
        while self.queue.jobs:
            if self.out_of_time():
                self.report.stopped_at_deadline = True
                return
            job = self.queue.jobs.popleft()
            fn = self.functions.get(job.function)
            if fn is None:
                self.report.failed += 1
                log.error("ops.unknown_job", job=job.function)
                continue
            kwargs = dict(job.kwargs)
            if job.function == "ingest_source" and len(job.args) == 1 and "since" not in kwargs:
                since = await _first_window_since(str(job.args[0]), first_window_days)
                if since is not None:
                    kwargs["since"] = since
            job_id = job.job_id or f"{TICK_JOB}:{job.function}"
            await self.call(job.function, fn, job.args, kwargs, job_id)


async def run_pass(
    settings: Settings,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    max_minutes: float = 50.0,
    first_window_days: int = 7,
    storage_limit_mb: float | None = None,
    channels: Any = None,
) -> TickReport:
    """Run the crons due since the previous pass and everything they enqueue."""
    from app.notify.channels import build_channels
    from app.runtime import build_runtime

    until = until or datetime.now(UTC)
    start = since or await last_tick_until() or until - timedelta(hours=1)
    report = TickReport(since=start.isoformat(), until=until.isoformat())
    report.database_mb = await database_megabytes()
    report.storage_guard = (
        storage_limit_mb is not None
        and report.database_mb >= storage_limit_mb * STORAGE_GUARD_RATIO
    )
    ctx: dict[str, Any] = {
        "runtime": build_runtime(settings, redis=None),
        "channels": channels if channels is not None else build_channels(settings),
        "redis": LocalQueue(),
        "job_try": 1,
    }
    work = Pass(ctx, report, deadline=time.monotonic() + max_minutes * 60)
    for job in due(cron_jobs(), start, until):
        name = job.name.removeprefix("cron:")
        if (report.storage_guard and name in INGEST_CRONS) or work.out_of_time():
            report.skipped_crons.append(name)
            continue
        report.crons.append(name)
        await work.call(name, job.coroutine, (), {}, f"cron:{name}:{until:%Y%m%d%H%M}")
    await work.drain(first_window_days)
    report.jobs = dict(sorted(work.counts.items()))
    report.pending_documents = await pending_documents()
    return report


def _raw_root(storage_url: str) -> Path | None:
    return Path(storage_url.removeprefix("file://")) if storage_url.startswith("file://") else None


async def prune_raw(settings: Settings) -> int:
    """Delete stored originals of documents that are no longer waiting to be processed.

    Only for a ``file://`` store that does not outlive the host (a CI runner): what a pass has
    processed is in PostgreSQL, and only the pending documents' originals are carried to the
    next pass. Refuses ``gs://``, where originals are kept to re-parse later."""
    root = _raw_root(settings.storage_url)
    if root is None or not root.is_dir():
        return 0
    async with session_scope() as s:
        keep = set(
            (
                await s.scalars(
                    select(Document.raw_uri).where(
                        Document.parse_status == "pending", Document.raw_uri.is_not(None)
                    )
                )
            ).all()
        )
    return remove_unlisted(root, {uri for uri in keep if uri})


def remove_unlisted(root: Path, keep: set[str]) -> int:
    """Delete every file under ``root`` whose ``file://`` URI (as ``store_raw`` writes it) is
    not in ``keep``, and the directories that leaves empty."""
    removed = 0
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file() and f"file://{path.resolve()}" not in keep:
            path.unlink()
            removed += 1
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    return removed


async def bootstrap(settings: Settings) -> dict[str, Any]:
    """Real reference data only, as scripts/render-runtime.py's migrate: the institution
    dictionary and the live sources, each enabled exactly when its key is configured. Never the
    fixture sources, demo organizations or accounts that ``manage seed`` creates."""
    from app.demo.seed import seed_institutions
    from app.runtime import build_runtime
    from app.sources.registry import SOURCE_CATALOG

    keys = {
        "g2b": bool(settings.data_go_kr_service_key),
        "clik": bool(settings.clik_api_key),
        "lofin": bool(settings.lofin_api_key),
        "crawler": False,  # 누리집 boards need per-institution configuration first
    }
    async with session_scope() as s:
        fixtures = await s.scalar(
            select(func.count()).select_from(Source).where(Source.key.like("fixture_%"))
        )
        if fixtures:
            # A database seeded for the demo mixes synthetic documents into all it reports.
            raise RuntimeError("this database has demo fixture sources; use a fresh database")
        institutions = await seed_institutions(s, build_runtime(settings, redis=None))
        for source in SOURCE_CATALOG:
            adapter = source["adapter"]
            # 999 items per 조달청 call, as the real 30-day run did (91 calls for 57,514 rows):
            # an hourly pass re-reads about four days, ~40 calls at the adapter's default 100.
            config = {"rows": 999} if adapter == "g2b" else {}
            statement = insert(Source).values(**source, enabled=keys[adapter], config=config)
            # A keyed source follows its key; a crawler keeps whatever an operator configured.
            await s.execute(
                statement.on_conflict_do_nothing(index_elements=["key"])
                if adapter == "crawler"
                else statement.on_conflict_do_update(
                    index_elements=["key"], set_={"enabled": statement.excluded.enabled}
                )
            )
        enabled = sorted(
            (await s.scalars(select(Source.key).where(Source.enabled.is_(True)))).all()
        )
    return {"institutions": institutions, "enabled_sources": enabled}


async def _unlock(lock: Any) -> None:
    """Release the pass lock. A session lock dies with its connection, so if the server
    already closed that connection there is nothing left to release: say so, don't fail a
    pass whose work is done and recorded."""
    try:
        await lock.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": TICK_LOCK})
    except DBAPIError as exc:
        log.warning("ops.tick.lock_connection_lost", error=f"{type(exc).__name__}: {exc}"[:300])


async def tick(
    settings: Settings,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    max_minutes: float = 50.0,
    first_window_days: int = 7,
    storage_limit_mb: float | None = None,
    prune: bool = False,
    channels: Any = None,
) -> TickReport:
    """A pass under the advisory lock, recorded as a ``job_runs`` row (Admin → Jobs) whose
    result says where the next pass starts."""
    from app.db.session import get_engine

    async with get_engine().connect() as conn:
        # Autocommit: the lock is held by the session, and a pass is long. In a transaction the
        # connection would sit "idle in transaction" for the whole pass, which hosted databases
        # end (the first Neon pass lost its lock connection after an hour and then failed).
        lock = await conn.execution_options(isolation_level="AUTOCOMMIT")
        if not await lock.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": TICK_LOCK}):
            now = datetime.now(UTC).isoformat()
            return TickReport(since=now, until=now, locked_out=True)
        try:
            async with session_scope() as s:
                run = JobRun(
                    job=TICK_JOB,
                    job_id=f"{TICK_JOB}:{datetime.now(UTC):%Y%m%d%H%M%S}",
                    status="running",
                )
                s.add(run)
                await s.flush()
                run_id = run.id
            status, error = "failed", None
            report: TickReport | None = None
            try:
                report = await run_pass(
                    settings,
                    since=since,
                    until=until,
                    max_minutes=max_minutes,
                    first_window_days=first_window_days,
                    storage_limit_mb=storage_limit_mb,
                    channels=channels,
                )
                if prune:
                    report.pruned_raw_files = await prune_raw(settings)
                status = "succeeded"
                return report
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:4000]
                raise
            finally:
                async with session_scope() as s:
                    row = await s.get(JobRun, run_id)
                    if row is not None:
                        row.status = status
                        row.error = error
                        row.finished_at = datetime.now(UTC)
                        row.result = report.as_dict() if report is not None else {}
        finally:
            await _unlock(lock)
