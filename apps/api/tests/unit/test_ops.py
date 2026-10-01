"""``manage ops tick``: the worker's own schedule and jobs in one process, without Redis."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from app.clock import KST
from app.ops import LocalQueue, cron_jobs, due, job_functions, remove_unlisted
from app.worker.queue import enqueue
from app.worker.settings import WorkerSettings


def _fired(since: datetime, until: datetime) -> list[str]:
    return [job.name.removeprefix("cron:") for job in due(cron_jobs(), since, until)]


def test_an_hourly_pass_runs_what_fired_since_the_previous_pass() -> None:
    fired = set(
        _fired(datetime(2026, 10, 1, 10, 23, tzinfo=KST), datetime(2026, 10, 1, 11, 23, tzinfo=KST))
    )
    assert {
        "ingest_procurement",  # minute 7
        "sweep_pending_documents",
        "sweep_pending_links",
        "deliver_notifications",
        "renew_subscriptions",  # minute 17
    } <= fired
    assert not fired & {
        "ingest_minutes",
        "ingest_budget_books",
        "daily_digest",
        "weekly_digest",
        "refresh_lifecycle",
        "nightly_backtest",
    }


def test_the_next_pass_does_not_fire_a_cron_again() -> None:
    fired = _fired(
        datetime(2026, 10, 1, 11, 23, tzinfo=KST), datetime(2026, 10, 1, 12, 6, tzinfo=KST)
    )
    assert "ingest_procurement" not in fired  # 12:07 has not come yet


def test_a_long_gap_catches_up_each_cron_once() -> None:
    # Saturday 01:00 → Monday 09:00: the weekly fetch (Sun 02:40) and digest (Mon 08:20) are due.
    fired = _fired(datetime(2026, 10, 3, 1, 0, tzinfo=KST), datetime(2026, 10, 5, 9, 0, tzinfo=KST))
    assert {"ingest_budget_books", "weekly_digest", "daily_digest", "ingest_minutes"} <= set(fired)
    assert len(fired) == len(set(fired))


def test_utc_times_are_read_on_the_workers_korean_clock() -> None:
    # 18:05 UTC → 18:15 UTC is 03:05 → 03:15 KST, when the daily minutes fetch fires (03:10).
    from datetime import UTC

    fired = _fired(
        datetime(2026, 10, 1, 18, 5, tzinfo=UTC), datetime(2026, 10, 1, 18, 15, tzinfo=UTC)
    )
    assert "ingest_minutes" in fired


def test_every_worker_job_can_run_in_a_pass() -> None:
    functions = job_functions()
    for entry in WorkerSettings.functions:
        assert getattr(entry, "name", getattr(entry, "__name__", None)) in functions
    # what the crons fan out to by name
    assert {"ingest_source", "process_document", "link_signals", "reconcile_links"} <= set(
        functions
    )


async def test_the_local_queue_refuses_a_job_id_already_taken() -> None:
    queue = LocalQueue()
    pool: Any = queue  # what the jobs pass to enqueue() as ctx["redis"]
    assert await enqueue(pool, "process_document", 1, job_id="process:1:sweep") == "process:1:sweep"
    assert await enqueue(pool, "process_document", 1, job_id="process:1:sweep") is None
    await queue.enqueue_job("link_signals", [2])
    await queue.enqueue_job("link_signals", [2])  # no id: nothing to de-duplicate on
    assert [(job.function, job.args) for job in queue.jobs] == [
        ("process_document", (1,)),
        ("link_signals", ([2],)),
        ("link_signals", ([2],)),
    ]


def test_pruning_keeps_only_the_originals_still_waiting(tmp_path: Path) -> None:
    waiting = tmp_path / "g2b_bid" / "ab" / "ab12.bin"
    done = tmp_path / "g2b_bid" / "cd" / "cd34.bin"
    for path in (waiting, done):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x")
    assert remove_unlisted(tmp_path, {f"file://{waiting.resolve()}"}) == 1
    assert waiting.exists()
    assert not done.exists()
    assert not done.parent.exists()  # emptied directories go too
