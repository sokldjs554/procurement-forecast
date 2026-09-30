"""The Postgres job queue: the SQL functions' contract, tenant isolation, and the worker loop
running a real media job through crash, resume, cancel, budget and shutdown."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.session import get_sessionmaker, session_scope
from app.media import worker as media_worker
from app.media.ffmpeg import Window
from app.media.stt import FixtureSTT, Segment
from app.queue.pg import cancel, claim, complete, enqueue, fail, heartbeat, reap
from app.queue.worker import Handler, JobContext, WorkerConfig, work_one


async def _org(name: str) -> int:
    async with session_scope() as s:
        return int(
            await s.scalar(
                text("INSERT INTO organizations (name) VALUES (:n) RETURNING id"), {"n": name}
            )
        )


def _kind() -> str:
    return f"test.{uuid.uuid4().hex[:8]}"


async def _job(job_id: int) -> dict[str, Any]:
    async with session_scope() as s:
        row = (await s.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id})).one()
    return dict(row._mapping)


async def test_enqueue_returns_the_live_job_for_the_same_key(migrated_db: str) -> None:
    org, kind = await _org("dedupe"), _kind()
    async with session_scope() as s:
        first = await enqueue(s, org_id=org, kind=kind, payload={"n": 1}, dedupe_key="v1")
        again = await enqueue(s, org_id=org, kind=kind, payload={"n": 2}, dedupe_key="v1")
        other = await enqueue(s, org_id=org, kind=kind, payload={}, dedupe_key="v2")
    assert first[1] and not again[1] and again[0] == first[0] and other[0] != first[0]

    async with session_scope() as s:  # a failed job frees its key
        job = await claim(s, "w", [kind], lease_seconds=30)
        assert job is not None and job.id == first[0]
        assert await fail(s, job.id, "w", "bad input", retryable=False) == "failed"
        retry = await enqueue(s, org_id=org, kind=kind, payload={}, dedupe_key="v1")
    assert retry[1] and retry[0] != first[0]


async def test_claim_skips_rows_another_worker_holds(migrated_db: str) -> None:
    org, kind = await _org("skip-locked"), _kind()
    async with session_scope() as s:
        a, _ = await enqueue(s, org_id=org, kind=kind, payload={})
        b, _ = await enqueue(s, org_id=org, kind=kind, payload={})
    maker = get_sessionmaker()
    async with maker() as one, maker() as two:
        first = await claim(one, "w1", [kind], lease_seconds=30)  # row lock held, not committed
        second = await asyncio.wait_for(claim(two, "w2", [kind], lease_seconds=30), timeout=5)
        third = await asyncio.wait_for(claim(two, "w3", [kind], lease_seconds=30), timeout=5)
        assert first is not None and second is not None
        assert {first.id, second.id} == {a, b} and third is None
        await one.commit()
        await two.commit()


async def test_heartbeat_tells_the_worker_to_stop(migrated_db: str) -> None:
    org, kind = await _org("heartbeat"), _kind()
    async with session_scope() as s:
        job_id, _ = await enqueue(s, org_id=org, kind=kind, payload={})
        await claim(s, "w1", [kind], lease_seconds=30)
        assert await heartbeat(s, job_id, "w1", lease_seconds=30, progress={"done": 1}) == "ok"
        assert await heartbeat(s, job_id, "w2", lease_seconds=30) == "lost"
        assert await cancel(s, job_id) == "cancelling"
        assert await heartbeat(s, job_id, "w1", lease_seconds=30) == "cancel"
        assert await fail(s, job_id, "w1", "stopped", retryable=True) == "cancelled"
    row = await _job(job_id)
    assert row["status"] == "cancelled" and row["progress"] == {"done": 1}
    assert row["locked_by"] is None and row["lease_until"] is None


async def test_failures_back_off_then_give_up(migrated_db: str) -> None:
    org, kind = await _org("backoff"), _kind()
    async with session_scope() as s:
        job_id, _ = await enqueue(s, org_id=org, kind=kind, payload={}, max_attempts=2)
        await claim(s, "w", [kind], lease_seconds=30)
        assert await fail(s, job_id, "w", "timeout", retryable=True, base_seconds=20) == "retry"
        delay = await s.scalar(
            text("SELECT extract(epoch FROM run_after - now()) FROM jobs WHERE id = :id"),
            {"id": job_id},
        )
        assert 9 <= float(delay) <= 21  # first retry: 20 s with equal jitter → 10–20 s
        assert await claim(s, "w", [kind], lease_seconds=30) is None  # not before its time
        await s.execute(text("UPDATE jobs SET run_after = now() WHERE id = :id"), {"id": job_id})
        again = await claim(s, "w", [kind], lease_seconds=30)
        assert again is not None and again.attempts == 2
        assert await fail(s, job_id, "w", "timeout", retryable=True) == "failed"
    assert (await _job(job_id))["status"] == "failed"


async def test_reaper_requeues_the_job_of_a_worker_that_vanished(migrated_db: str) -> None:
    org, kind = await _org("reap"), _kind()
    async with session_scope() as s:
        job_id, _ = await enqueue(s, org_id=org, kind=kind, payload={})
        await claim(s, "dead", [kind], lease_seconds=30)
        await s.execute(
            text("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = :id"),
            {"id": job_id},
        )
        assert await reap(s) >= 1
        # The dead worker's late result is refused; the job is someone else's now.
        assert not await complete(s, job_id, "dead", {"late": True}, {})
    row = await _job(job_id)
    assert row["status"] == "queued" and "dead stopped heartbeating" in row["error"]


async def test_cancel_reaches_every_job_under_the_one_cancelled(migrated_db: str) -> None:
    org, kind = await _org("cancel-tree"), _kind()
    async with session_scope() as s:
        parent, _ = await enqueue(s, org_id=org, kind=kind, payload={})
        await claim(s, "w", [kind], lease_seconds=30)
        child, _ = await enqueue(s, org_id=org, kind=kind, payload={}, parent_id=parent)
        assert await cancel(s, parent) == "cancelling"
    assert (await _job(child))["status"] == "cancelled"
    assert (await _job(parent))["cancel_requested"]


async def test_a_job_is_billed_once_however_often_it_is_finished(migrated_db: str) -> None:
    from app.queue.pg import record_usage

    org, kind = await _org("bill-once"), _kind()
    async with session_scope() as s:
        job_id, _ = await enqueue(s, org_id=org, kind=kind, payload={})
        await claim(s, "w", [kind], lease_seconds=30)
    for _ in range(2):  # a retried completion, or two workers that both finished
        async with session_scope() as s:
            await record_usage(
                s, org_id=org, job_id=job_id, meter="stt_audio_seconds", quantity=60, usd=0.36
            )
            await complete(s, job_id, "w", {}, {})
    async with session_scope() as s:
        rows = (
            await s.execute(
                text("SELECT count(*) AS n, sum(usd) AS usd FROM usage_events WHERE job_id = :j"),
                {"j": job_id},
            )
        ).one()
    assert rows.n == 1 and float(rows.usd) == pytest.approx(0.36)


async def _as_tenant(s: Any, org: int) -> None:
    await s.execute(text("SET LOCAL ROLE app_tenant"))
    await s.execute(text("SELECT set_config('app.org_id', :org, true)"), {"org": str(org)})


async def _tenant_query(org: int | None, statement: str, params: dict[str, Any]) -> None:
    async with session_scope() as s:
        if org is None:
            await s.execute(text("SET LOCAL ROLE app_tenant"))
        else:
            await _as_tenant(s, org)
        await s.execute(text(statement), params)


async def test_a_tenant_session_sees_and_touches_only_its_own_rows(migrated_db: str) -> None:
    mine, theirs = await _org("tenant-a"), await _org("tenant-b")
    kind = _kind()
    async with session_scope() as s:
        their_job, _ = await enqueue(s, org_id=theirs, kind=kind, payload={"secret": 1})
        await s.execute(
            text(
                "INSERT INTO usage_events (org_id, meter, quantity, usd, idempotency_key)"
                " VALUES (:org, 'ocr_frames', 3, 0, :key)"
            ),
            {"org": theirs, "key": f"t-{uuid.uuid4()}"},
        )
    async with session_scope() as s:
        await _as_tenant(s, mine)
        my_job, created = await enqueue(s, org_id=mine, kind=kind, payload={})
        assert created
        seen = (await s.scalars(text("SELECT id FROM jobs WHERE kind = :k"), {"k": kind})).all()
        assert seen == [my_job]
        assert await cancel(s, their_job) == "not_found"
        usage = await s.scalar(
            text("SELECT count(*) FROM usage_events WHERE org_id = :o"), {"o": theirs}
        )
        assert usage == 0
    for statement, params in (
        # writing a row for another tenant
        ("SELECT * FROM jobq_enqueue(:o, 'x', '{}'::jsonb)", {"o": theirs}),
        # working the queue is the workers' job, not a tenant's
        ("SELECT * FROM jobq_claim('w', ARRAY['x'], 30)", {}),
        # keys and checkpoints are invisible, not just filtered
        ("SELECT count(*) FROM api_keys", {}),
    ):
        with pytest.raises(DBAPIError):
            await _tenant_query(mine, statement, params)
    with pytest.raises(DBAPIError, match=r"app\.org_id|invalid input"):
        await _tenant_query(None, "SELECT count(*) FROM jobs", {})  # nobody said who asks
    assert (await _job(their_job))["status"] == "queued"


# --- the worker on a real media job --------------------------------------------------------


class _ScriptedSTT(FixtureSTT):
    """Counts calls; can crash on one, and can be slow (to leave room for heartbeats)."""

    def __init__(
        self,
        segments: list[Segment],
        *,
        crash_on: int | None = None,
        delay: float = 0.0,
        usd_per_audio_minute: float = 0.0,
    ) -> None:
        super().__init__(segments, usd_per_audio_minute=usd_per_audio_minute)
        self.calls = 0
        self._crash_on, self._delay = crash_on, delay

    async def transcribe(self, audio: Path, window: Window, *, language: str) -> list[Segment]:
        self.calls += 1
        if self.calls == self._crash_on:
            raise RuntimeError("STT provider returned 503")
        await asyncio.sleep(self._delay)
        return await super().transcribe(audio, window, language=language)


@pytest.fixture
def media_runtime(runtime: Any, tmp_path: Path) -> Any:
    settings = runtime.settings.model_copy(
        update={
            "media_workdir": str(tmp_path / "media"),
            "media_window_seconds": 10.0,
            "media_window_search_seconds": 3.0,
        }
    )
    return dataclasses.replace(runtime, settings=settings)


def _use_stt(monkeypatch: pytest.MonkeyPatch, stt: FixtureSTT) -> None:
    monkeypatch.setattr(media_worker, "stt_from_spec", lambda *a, **k: stt)


def _handlers(runtime: Any, kind: str) -> dict[str, Handler]:
    async def handle(job: Any, ctx: JobContext) -> dict[str, Any]:
        return await media_worker.transcribe_job(job, ctx, runtime=runtime)

    return {kind: handle}


async def _enqueue_meeting(org: int, kind: str, meeting_video: Any, **kw: Any) -> int:
    async with session_scope() as s:
        job_id, _ = await enqueue(
            s,
            org_id=org,
            kind=kind,
            payload={
                "video": f"file://{meeting_video.video}",
                "title": "제300회 도시건설위원회 제2차 회의",
                "meeting_date": "2026-03-18",
                "publisher": "경기도 성남시의회",
                "external_id": f"queue-{org}",
            },
            **kw,
        )
    return job_id


async def _until_running(job_id: int) -> None:
    for _ in range(600):
        if (await _job(job_id))["status"] == "running":
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} never started")


def _config(name: str, **kw: Any) -> WorkerConfig:
    return WorkerConfig(worker_id=name, heartbeat_seconds=0.1, **kw)


async def _usage(job_id: int) -> dict[str, tuple[float, float]]:
    async with session_scope() as s:
        rows = (
            await s.execute(
                text("SELECT meter, quantity, usd FROM usage_events WHERE job_id = :id"),
                {"id": job_id},
            )
        ).all()
    return {r.meter: (float(r.quantity), float(r.usd)) for r in rows}


async def test_worker_runs_a_media_job_and_meters_it(  # type: ignore[no-untyped-def]
    demo_world, media_runtime, meeting_video, monkeypatch
) -> None:
    segments = [Segment.from_json(x) for x in meeting_video.segments]
    stt = _ScriptedSTT(segments, usd_per_audio_minute=0.006)
    _use_stt(monkeypatch, stt)
    org, kind = await _org("media-e2e"), _kind()
    job_id = await _enqueue_meeting(org, kind, meeting_video)

    assert await work_one(_handlers(media_runtime, kind), _config("w-e2e")) == "succeeded"
    row = await _job(job_id)
    assert row["status"] == "succeeded" and row["attempts"] == 1
    flood = [
        x for x in row["result"]["signals"] if "지하차도" in x["title"] or "차단" in x["title"]
    ]
    assert flood and flood[0]["verdict"] == "accepted" and 12.0 <= flood[0]["t0"] < 22.0
    usage = await _usage(job_id)
    assert set(usage) == {"stt_audio_seconds", "ocr_frames", "compute_seconds"}
    seconds, usd = usage["stt_audio_seconds"]
    assert seconds == pytest.approx(meeting_video.duration, abs=0.1)
    assert usd == pytest.approx(seconds / 60 * 0.006, rel=1e-3)
    assert row["cost"]["audio_seconds_sent"] == pytest.approx(seconds, abs=0.01)


async def test_a_crashed_media_job_resumes_from_postgres_checkpoints(  # type: ignore[no-untyped-def]
    demo_world, media_runtime, meeting_video, monkeypatch
) -> None:
    segments = [Segment.from_json(x) for x in meeting_video.segments]
    org, kind = await _org("media-resume"), _kind()
    job_id = await _enqueue_meeting(org, kind, meeting_video)

    _use_stt(monkeypatch, crashing := _ScriptedSTT(segments, crash_on=3))
    assert await work_one(_handlers(media_runtime, kind), _config("w-crash")) == "retry"
    row = await _job(job_id)
    assert row["status"] == "queued" and "503" in row["error"]

    async with session_scope() as s:  # skip the backoff wait
        await s.execute(text("UPDATE jobs SET run_after = now() WHERE id = :id"), {"id": job_id})
    _use_stt(monkeypatch, second := _ScriptedSTT(segments))
    assert await work_one(_handlers(media_runtime, kind), _config("w-resume")) == "succeeded"
    media = (await _job(job_id))["result"]["media"]
    assert crashing.calls == 3 and media["resumed_windows"] == 2
    assert second.calls == media["windows"] - 2


async def test_cancelling_a_running_media_job_stops_it(  # type: ignore[no-untyped-def]
    demo_world, media_runtime, meeting_video, monkeypatch
) -> None:
    segments = [Segment.from_json(x) for x in meeting_video.segments]
    _use_stt(monkeypatch, _ScriptedSTT(segments, delay=0.5))
    org, kind = await _org("media-cancel"), _kind()
    job_id = await _enqueue_meeting(org, kind, meeting_video)
    running = asyncio.create_task(work_one(_handlers(media_runtime, kind), _config("w-cancel")))
    await _until_running(job_id)
    async with session_scope() as s:
        assert await cancel(s, job_id) == "cancelling"
    assert await asyncio.wait_for(running, timeout=30) == "cancelled"
    row = await _job(job_id)
    assert row["status"] == "cancelled" and row["error"] == "cancelled by request"
    assert await _usage(job_id) == {}


async def test_budget_is_checked_before_any_audio_is_sent(  # type: ignore[no-untyped-def]
    demo_world, media_runtime, meeting_video, monkeypatch
) -> None:
    segments = [Segment.from_json(x) for x in meeting_video.segments]
    _use_stt(monkeypatch, stt := _ScriptedSTT(segments, usd_per_audio_minute=1.0))
    org, kind = await _org("media-budget"), _kind()
    job_id = await _enqueue_meeting(org, kind, meeting_video, budget_usd=0.10)
    assert await work_one(_handlers(media_runtime, kind), _config("w-budget")) == "failed"
    row = await _job(job_id)
    assert "exceeds the job budget" in row["error"] and row["attempts"] == 1
    assert stt.calls == 0 and await _usage(job_id) == {}

    # The organisation's monthly cap counts what it already spent.
    async with session_scope() as s:
        await s.execute(
            text("UPDATE organizations SET usage_cap_usd = 1.00 WHERE id = :o"), {"o": org}
        )
        await s.execute(
            text(
                "INSERT INTO usage_events (org_id, meter, quantity, usd, idempotency_key)"
                " VALUES (:o, 'stt_audio_seconds', 30, 0.6, :k)"
            ),
            {"o": org, "k": f"prior-{org}"},
        )
    capped = await _enqueue_meeting(org, kind, meeting_video, dedupe_key="cap")
    assert await work_one(_handlers(media_runtime, kind), _config("w-cap")) == "failed"
    assert "this month exceeds the cap" in (await _job(capped))["error"]


async def test_a_worker_shut_down_mid_job_hands_it_back(  # type: ignore[no-untyped-def]
    demo_world, media_runtime, meeting_video, monkeypatch
) -> None:
    segments = [Segment.from_json(x) for x in meeting_video.segments]
    _use_stt(monkeypatch, _ScriptedSTT(segments, delay=1.0))
    org, kind = await _org("media-shutdown"), _kind()
    job_id = await _enqueue_meeting(org, kind, meeting_video)
    running = asyncio.create_task(work_one(_handlers(media_runtime, kind), _config("w-stop")))
    await _until_running(job_id)
    running.cancel()  # SIGTERM
    with pytest.raises(asyncio.CancelledError):
        await running
    row = await _job(job_id)
    assert row["status"] == "queued" and row["error"] == "worker shut down mid-job"
    async with session_scope() as s:
        wait = await s.scalar(
            text("SELECT extract(epoch FROM run_after - now()) FROM jobs WHERE id = :id"),
            {"id": job_id},
        )
    assert float(wait) <= 1.0  # back in the queue now, not after the lease runs out


async def test_a_shutdown_right_after_the_claim_commits_hands_the_job_back(
    migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The narrowest window: the claim is committed, the handler has not started."""
    from contextlib import asynccontextmanager

    from app.queue import worker as queue_worker

    org, kind = await _org("claim-window"), _kind()
    async with session_scope() as s:
        job_id, _ = await enqueue(s, org_id=org, kind=kind, payload={})
    committed = asyncio.Event()
    real_scope, calls = queue_worker.session_scope, 0

    @asynccontextmanager
    async def pause_after_first_commit():  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        async with real_scope() as s:
            yield s
        if calls == 1:
            committed.set()
            await asyncio.sleep(30)

    monkeypatch.setattr(queue_worker, "session_scope", pause_after_first_commit)

    async def never(job: Any, ctx: JobContext) -> dict[str, Any]:
        raise AssertionError("the handler must not start")

    running = asyncio.create_task(work_one({kind: never}, _config("w-window")))
    await asyncio.wait_for(committed.wait(), timeout=10)
    assert (await _job(job_id))["status"] == "running"
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    row = await _job(job_id)
    assert row["status"] == "queued" and row["error"] == "worker shut down mid-job"


async def test_a_worker_that_lost_its_lease_writes_nothing(  # type: ignore[no-untyped-def]
    demo_world, media_runtime, meeting_video, monkeypatch
) -> None:
    segments = [Segment.from_json(x) for x in meeting_video.segments]
    _use_stt(monkeypatch, _ScriptedSTT(segments, delay=0.5))
    org, kind = await _org("media-lost"), _kind()
    job_id = await _enqueue_meeting(org, kind, meeting_video)
    config = WorkerConfig(worker_id="w-paused", heartbeat_seconds=1.5, lease_seconds=1)
    running = asyncio.create_task(work_one(_handlers(media_runtime, kind), config))
    await _until_running(job_id)
    await asyncio.sleep(1.2)  # the lease (1 s) runs out before the first heartbeat (1.5 s)
    async with session_scope() as s:
        assert await reap(s) >= 1
    assert await asyncio.wait_for(running, timeout=30) == "lost"
    row = await _job(job_id)
    assert row["status"] == "queued" and row["result"] is None
    assert await _usage(job_id) == {}


def test_payload_is_json_safe() -> None:
    # Decimal budgets and dates in results must survive the trip into jsonb.
    from decimal import Decimal

    from app.queue.pg import _json

    assert json.loads(_json({"usd": Decimal("0.10")})) == {"usd": "0.10"}
