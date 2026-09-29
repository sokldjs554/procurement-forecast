import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from arq import Retry
from sqlalchemy.dialects.sqlite import dialect
from sqlalchemy.exc import DBAPIError
from sqlalchemy.sql.dml import Update

from app.pipeline.link_reconcile import LinkSnapshotChangedError
from app.worker import tasks


class Rows:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def all(self) -> list[Any]:
        return self.rows


class Session:
    def __init__(self, owners: list[str | None] | None = None) -> None:
        self.owners = owners if owners is not None else ["LG-1"]
        self.pending = [("LG-1", 4)]
        self.state = SimpleNamespace(reconciled_generation=4, recommendations_generation=0)
        self.active = False
        self.commits = 0
        self.updates: list[Any] = []

    async def scalars(self, stmt: Any) -> Rows:
        if "opportunities.institution_code" in str(stmt):
            return Rows(self.owners)
        assert "organizations.id" in str(stmt)
        return Rows([11, 12])

    async def execute(self, stmt: Any) -> Rows:
        if isinstance(stmt, Update):
            self.updates.append(stmt)
        return Rows(self.pending)

    async def get(self, model: Any, key: str) -> Any:
        assert key == "LG-1"
        return self.state

    @asynccontextmanager
    async def scope(self) -> AsyncIterator["Session"]:
        self.active = True
        try:
            yield self
            self.commits += 1
        finally:
            self.active = False


class Queue:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.jobs: dict[str, tuple[str, tuple[Any, ...]]] = {}
        self.fail_once = False

    async def enqueue_job(self, name: str, *args: Any, **kwargs: Any) -> Any:
        assert not self.session.active, "Downstream work must see committed reconciliation"
        job_id = kwargs["_job_id"]
        if self.fail_once and args == (12,):
            self.fail_once = False
            raise RuntimeError("queue unavailable after the first organization")
        if job_id in self.jobs:
            return None
        self.jobs[job_id] = (name, args)
        return SimpleNamespace(job_id=job_id)


def setup_worker(monkeypatch: pytest.MonkeyPatch, session: Session) -> dict[str, Any]:
    monkeypatch.setattr(tasks, "session_scope", session.scope)
    monkeypatch.setattr(tasks, "latest_calibration", AsyncMock(return_value=None))
    monkeypatch.setattr(tasks, "link_signals_impl", AsyncMock(return_value=[101]))
    return {"runtime": object(), "redis": Queue(session)}


async def test_provisional_link_queues_institution_reconciliation_before_recommendations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    ctx = setup_worker(monkeypatch, session)
    await tasks.link_signals.__wrapped__(ctx, [1])
    assert list(ctx["redis"].jobs.values()) == [("reconcile_links", ("LG-1",))]
    assert list(ctx["redis"].jobs) == ["reconcile:LG-1:4"]


async def test_unresolved_owner_keeps_recommendation_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session(owners=[None])
    session.pending = []
    ctx = setup_worker(monkeypatch, session)
    await tasks.link_signals.__wrapped__(ctx, [1])
    assert [name for name, _ in ctx["redis"].jobs.values()] == [
        "refresh_recommendations",
        "refresh_recommendations",
    ]


async def test_reconciliation_retry_recovers_partial_enqueue_without_duplicate_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    ctx = setup_worker(monkeypatch, session)
    ctx["redis"].fail_once = True
    results = [
        SimpleNamespace(
            touched_ids=[101],
            protected_opportunities=0,
            changed_signals=0,
            generation=4,
            processed=True,
        ),
        SimpleNamespace(
            touched_ids=[],
            protected_opportunities=0,
            changed_signals=0,
            generation=4,
            processed=False,
        ),
    ]
    monkeypatch.setattr(
        tasks, "reconcile_institution", AsyncMock(side_effect=results), raising=False
    )
    with pytest.raises(RuntimeError, match="queue unavailable"):
        await tasks.reconcile_links.__wrapped__(ctx, "LG-1")
    assert session.updates == []
    await tasks.reconcile_links.__wrapped__(ctx, "LG-1")
    assert ctx["redis"].jobs == {
        "recs:LG-1:4:11": ("refresh_recommendations", (11,)),
        "recs:LG-1:4:12": ("refresh_recommendations", (12,)),
    }
    assert len(session.updates) == 1
    assert "recommendations_generation" in str(session.updates[0])
    assert "greatest" in str(session.updates[0]).lower()


async def test_clean_reconciliation_does_not_repeat_recommendations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    session.state.recommendations_generation = 4
    ctx = setup_worker(monkeypatch, session)
    monkeypatch.setattr(
        tasks,
        "reconcile_institution",
        AsyncMock(
            return_value=SimpleNamespace(
                touched_ids=[],
                protected_opportunities=0,
                changed_signals=0,
                generation=4,
                processed=False,
            )
        ),
        raising=False,
    )
    await tasks.reconcile_links.__wrapped__(ctx, "LG-1")
    assert ctx["redis"].jobs == {}
    assert session.updates == []


async def test_sweep_uses_durable_generation_to_deduplicate_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    ctx = setup_worker(monkeypatch, session)
    await tasks.sweep_pending_links.__wrapped__(ctx)
    await tasks.sweep_pending_links.__wrapped__(ctx)
    assert ctx["redis"].jobs == {"reconcile:LG-1:4": ("reconcile_links", ("LG-1",))}


def test_reconciliation_sweep_runs_each_minute() -> None:
    from app.worker.settings import WorkerSettings

    sweep = next(
        job for job in WorkerSettings.cron_jobs if job.coroutine.__name__ == "sweep_pending_links"
    )
    assert sweep.minute == set(range(60))
    reconcile = next(
        job for job in WorkerSettings.functions if getattr(job, "name", None) == "reconcile_links"
    )
    assert reconcile.keep_result_s == 0


async def test_pending_query_recovers_normalization_and_dispatch_gaps_with_a_bounded_page() -> None:
    # Execute the integer-only recovery predicate, including its real scope/limit, without
    # substituting prefiltered rows. PostgreSQL transaction behavior is covered in CI.
    with sqlite3.connect(":memory:") as connection:
        connection.execute(
            "CREATE TABLE link_reconciliation_states (institution_code TEXT, generation INTEGER, "
            "reconciled_generation INTEGER, recommendations_generation INTEGER)"
        )
        connection.executemany(
            "INSERT INTO link_reconciliation_states VALUES (?, ?, ?, ?)",
            [("clean", 4, 4, 4), ("normalize", 5, 4, 4), ("dispatch", 4, 4, 3)],
        )

        class QuerySession:
            async def execute(self, stmt: Any) -> Rows:
                sql = str(stmt.compile(dialect=dialect(), compile_kwargs={"literal_binds": True}))
                return Rows(connection.execute(sql).fetchall())

        session = QuerySession()
        assert await tasks._pending_link_generations(session) == [("dispatch", 4), ("normalize", 5)]
        assert await tasks._pending_link_generations(session, {"dispatch"}) == [("dispatch", 4)]
        connection.executemany(
            "INSERT INTO link_reconciliation_states VALUES (?, ?, ?, ?)",
            [(f"pending-{i:03}", 1, 0, 0) for i in range(250)],
        )
        assert len(await tasks._pending_link_generations(session)) == 200


@pytest.mark.parametrize("kind", ["snapshot", "40001", "40P01", "55P03"])
async def test_tracking_retries_reconciliation_conflicts_with_backoff(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    class TrackingSession(Session):
        def add(self, row: Any) -> None:
            row.id = 9
            self.row = row

        async def flush(self) -> None:
            return None

        async def get(self, model: Any, key: Any) -> Any:
            return self.row

    session = TrackingSession()
    monkeypatch.setattr(tasks, "session_scope", session.scope)
    if kind == "snapshot":
        error: Exception = LinkSnapshotChangedError("changed evidence")
    else:
        original = RuntimeError("retryable transaction conflict")
        original.sqlstate = kind  # type: ignore[attr-defined]
        error = DBAPIError(None, None, original)

    @tasks.tracked("reconcile_links")
    async def conflict(ctx: dict[str, Any]) -> None:
        raise error

    with pytest.raises(Retry) as first:
        await conflict({"job_try": 1})
    assert first.value.defer_score > 0
    assert session.row.status == "retrying"
    with pytest.raises(type(error)):
        await conflict({"job_try": tasks.MAX_TRIES})
    assert session.row.status == "failed"
