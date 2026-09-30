"""The Python stage worker: claim, run, keep the lease alive, finish exactly once.

While a handler runs, a heartbeat renews the lease and publishes progress. The heartbeat is
also how a worker learns to stop: ``cancel`` (someone asked) or ``lost`` (the lease expired and
the job went back to the queue — this worker must not write a result another worker will
also write). A worker that is shut down hands its job back at once instead of letting the
lease run out.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from app.db.session import session_scope
from app.log import get_logger
from app.queue.pg import Job, claim, complete, fail, heartbeat, reap, record_usage
from app.settings import get_settings

log = get_logger(__name__)


class PermanentError(Exception):
    """A failure no retry can fix (bad input, over budget): the job fails now."""


@dataclass(slots=True)
class JobContext:
    job: Job
    worker: str
    progress: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    # (meter, quantity, usd), written with the job's completion in one transaction
    usage: list[tuple[str, float, float]] = field(default_factory=list)

    async def report(self, stage: str, done: int, total: int) -> None:
        self.progress = {"stage": stage, "done": done, "total": total}


Handler = Callable[[Job, JobContext], Coroutine[Any, Any, dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    worker_id: str
    lease_seconds: int = 60
    heartbeat_seconds: float = 15.0
    poll_seconds: float = 5.0
    reap_seconds: float = 30.0
    backoff_base_seconds: int = 15
    backoff_cap_seconds: int = 3600


async def _hand_back(job: Job, config: WorkerConfig, error: str) -> None:
    async with session_scope() as s:
        await fail(
            s,
            job.id,
            config.worker_id,
            error,
            retryable=True,
            base_seconds=1,
            cap_seconds=1,
        )


async def work_one(handlers: dict[str, Handler], config: WorkerConfig) -> str | None:
    """Claim and run one job. Returns ``succeeded``, ``retry``, ``failed``, ``cancelled`` or
    ``lost``, or ``None`` when nothing was ready."""
    job: Job | None = None
    try:
        async with session_scope() as s:
            job = await claim(
                s, config.worker_id, list(handlers), lease_seconds=config.lease_seconds
            )
    except asyncio.CancelledError:
        # Shut down while the claim committed: hand it back (a no-op if it never committed).
        if job is not None:
            await asyncio.shield(_hand_back(job, config, "worker shut down mid-job"))
        raise
    if job is None:
        return None
    return await _run_claimed(job, handlers[job.kind], config)


async def _run_claimed(job: Job, handler: Handler, config: WorkerConfig) -> str:
    # No await between here and the try below: a shutdown cannot land in between.
    ctx = JobContext(job, config.worker_id)
    work = asyncio.create_task(handler(job, ctx))
    stop: str | None = None

    async def keep_alive() -> None:
        nonlocal stop
        while True:
            await asyncio.sleep(config.heartbeat_seconds)
            try:
                async with session_scope() as s:
                    state = await heartbeat(
                        s,
                        job.id,
                        config.worker_id,
                        lease_seconds=config.lease_seconds,
                        progress=ctx.progress or None,
                    )
            except Exception as exc:  # the database blinked; the lease has slack for this
                log.warning("queue.heartbeat_failed", job_id=job.id, error=repr(exc)[:300])
                continue
            if state != "ok":
                stop = state
                work.cancel()
                return

    beat = asyncio.create_task(keep_alive())
    try:
        log.info("queue.claimed", job_id=job.id, kind=job.kind, attempt=job.attempts)
        result = await work
    except asyncio.CancelledError:
        if stop is None:  # this worker is shutting down: give the job back now
            await asyncio.shield(_hand_back(job, config, "worker shut down mid-job"))
            raise
        if stop == "lost":
            log.warning("queue.lease_lost", job_id=job.id)
            return "lost"
        async with session_scope() as s:
            return await fail(
                s, job.id, config.worker_id, "cancelled by request", retryable=False, cost=ctx.cost
            )
    except Exception as exc:
        permanent = isinstance(exc, PermanentError)
        async with session_scope() as s:
            outcome = await fail(
                s,
                job.id,
                config.worker_id,
                f"{type(exc).__name__}: {exc}",
                retryable=not permanent,
                cost=ctx.cost or None,
                base_seconds=config.backoff_base_seconds,
                cap_seconds=config.backoff_cap_seconds,
            )
        log.warning("queue.job_failed", job_id=job.id, outcome=outcome, error=repr(exc)[:300])
        return outcome
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
    # The work is done and paid for: a shutdown now must not throw the result away.
    return await asyncio.shield(_finish(job, ctx, result, config))


async def _finish(job: Job, ctx: JobContext, result: dict[str, Any], config: WorkerConfig) -> str:
    async with session_scope() as s:
        for meter, quantity, usd in ctx.usage:
            await record_usage(
                s, org_id=job.org_id, job_id=job.id, meter=meter, quantity=quantity, usd=usd
            )
        if not await complete(s, job.id, config.worker_id, result, ctx.cost):
            await s.rollback()  # reaped while finishing: the next owner records its own
            log.warning("queue.lease_lost_at_completion", job_id=job.id)
            return "lost"
    log.info("queue.succeeded", job_id=job.id, kind=job.kind)
    return "succeeded"


async def _listen(kinds: list[str], wake: asyncio.Event) -> Any:
    """LISTEN for new jobs so an idle worker starts within milliseconds, not a poll period.
    Best effort: without it the worker still polls."""
    import asyncpg

    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    try:
        conn = await asyncpg.connect(dsn)
        await conn.add_listener("jobq", lambda *args: wake.set() if args[3] in kinds else None)
    except (OSError, asyncpg.PostgresError) as exc:
        log.warning("queue.listen_unavailable", error=repr(exc)[:300])
        return None
    return conn


async def run_worker(
    handlers: dict[str, Handler], config: WorkerConfig, stop: asyncio.Event
) -> None:
    wake = asyncio.Event()
    listener = await _listen(list(handlers), wake)
    last_reap = 0.0
    try:
        while not stop.is_set():
            if time.monotonic() - last_reap >= config.reap_seconds:
                async with session_scope() as s:
                    if reaped := await reap(
                        s,
                        base_seconds=config.backoff_base_seconds,
                        cap_seconds=config.backoff_cap_seconds,
                    ):
                        log.warning("queue.reaped", jobs=reaped)
                last_reap = time.monotonic()
            if await work_one(handlers, config) is not None:
                continue
            wake.clear()
            waiters = [asyncio.create_task(wake.wait()), asyncio.create_task(stop.wait())]
            await asyncio.wait(waiters, timeout=config.poll_seconds, return_when="FIRST_COMPLETED")
            for waiter in waiters:
                waiter.cancel()
    finally:
        if listener is not None:
            await listener.close()
