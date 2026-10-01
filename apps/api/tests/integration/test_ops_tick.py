"""A scheduled pass against PostgreSQL: pending work gets done, the pass is recorded, and the next
pass starts where the previous one stopped."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest
from sqlalchemy import select, text

from app.clock import KST
from app.db.models import Document, JobRun, Source
from app.db.session import get_engine, session_scope
from app.ops import TICK_JOB, TICK_LOCK, TickReport, bootstrap, tick
from app.pipeline.ingest import upsert_record
from app.settings import get_settings
from app.sources.base import RawRecord

MINUTES = """제301회 서울특별시 강남구의회 임시회
행정재무위원회 회의록
일시: 2026년 9월 18일(금) 10시 00분

○위원 김민수 버스정류장 스마트쉘터 확충 계획을 여쭙겠습니다.
○교통행정과장 이정훈 내년도 본예산에 스마트쉘터 7개소 추가 설치 사업비 3억 5천만원을 반영하겠습니다.
"""


class Quiet:
    """Accepts whatever the shared database still has to deliver."""

    kind = "test"

    async def send(self, target: str, payload: dict[str, Any]) -> None:
        return None


CHANNELS = {kind: Quiet() for kind in ("email", "slack", "kakao")}


async def test_a_pass_processes_what_is_pending_and_the_next_starts_where_it_stopped(  # type: ignore[no-untyped-def]
    demo_world, runtime
) -> None:
    async with session_scope() as s:
        source = await s.scalar(select(Source).where(Source.key == "fixture_minutes"))
        assert source is not None
        doc, status = await upsert_record(
            s,
            source,
            RawRecord(
                external_id="ops-tick-minutes-1",
                doc_type="council_minutes",
                title="제301회 서울특별시 강남구의회 임시회 행정재무위원회 회의록",
                published_at=date(2026, 9, 20),
                mime="text/plain",
                publisher_raw="서울특별시 강남구의회",
                content=MINUTES.encode(),
            ),
            runtime,
        )
        doc_id = doc.id
    assert status == "created"

    # 10:41 → 10:50:30 KST: only the sweeps and delivery fire, not a fetch or a digest.
    since = datetime(2026, 10, 1, 10, 41, tzinfo=KST)
    until = datetime(2026, 10, 1, 10, 50, 30, tzinfo=KST)
    report = await tick(get_settings(), since=since, until=until, channels=CHANNELS)
    assert report.crons == [
        "sweep_pending_documents",
        "sweep_pending_links",
        "deliver_notifications",
    ]
    assert report.jobs.get("process_document", 0) >= 1
    assert not report.locked_out
    async with session_scope() as s:
        processed = await s.get(Document, doc_id)
        run = await s.scalar(
            select(JobRun).where(JobRun.job == TICK_JOB).order_by(JobRun.id.desc()).limit(1)
        )
    assert processed is not None and processed.parse_status != "pending"
    assert run is not None and run.status == "succeeded"
    assert run.result["until"] == until.isoformat()
    assert run.result["jobs"]["process_document"] == report.jobs["process_document"]

    later = await tick(
        get_settings(), until=datetime(2026, 10, 1, 10, 52, tzinfo=KST), channels=CHANNELS
    )
    assert later.since == until.isoformat()
    assert "sweep_pending_documents" not in later.crons  # fired at 10:50; next at 11:00


async def test_a_pass_that_overlaps_another_does_nothing(demo_world) -> None:  # type: ignore[no-untyped-def]
    async with get_engine().connect() as other:
        await other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": TICK_LOCK})
        try:
            report = await tick(get_settings(), channels=CHANNELS)
        finally:
            await other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": TICK_LOCK})
    assert report.locked_out
    assert not report.crons and not report.jobs


async def test_bootstrap_refuses_a_database_seeded_for_the_demo(demo_world) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(RuntimeError, match="fixture"):
        await bootstrap(get_settings())


async def _lock_backend(other) -> tuple[int, str]:  # type: ignore[no-untyped-def]
    row = (
        await other.execute(
            text(
                "SELECT a.pid, a.state FROM pg_locks l JOIN pg_stat_activity a USING (pid) "
                "WHERE l.locktype = 'advisory' AND l.objid = :k AND l.granted"
            ),
            {"k": TICK_LOCK},
        )
    ).one()
    return row.pid, row.state


async def test_the_lock_connection_holds_no_transaction_through_a_long_pass(  # type: ignore[no-untyped-def]
    demo_world, monkeypatch
) -> None:
    seen: list[str] = []

    async def pass_(*args: Any, **kwargs: Any) -> TickReport:
        async with get_engine().connect() as other:
            seen.append((await _lock_backend(other))[1])
        return TickReport(since="s", until="u")

    monkeypatch.setattr("app.ops.run_pass", pass_)
    await tick(get_settings(), channels=CHANNELS)
    # "idle in transaction" for an hour is what a hosted database ends.
    assert seen == ["idle"]


async def test_a_pass_whose_lock_connection_the_server_closed_still_finishes(  # type: ignore[no-untyped-def]
    demo_world, monkeypatch
) -> None:
    async def pass_(*args: Any, **kwargs: Any) -> TickReport:
        async with get_engine().connect() as other:
            pid, _ = await _lock_backend(other)
            await other.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
        return TickReport(since="s", until="u", jobs={"process_document": 3})

    monkeypatch.setattr("app.ops.run_pass", pass_)
    report = await tick(get_settings(), channels=CHANNELS)
    assert report.jobs == {"process_document": 3}
    async with session_scope() as s:
        run = await s.scalar(
            select(JobRun).where(JobRun.job == TICK_JOB).order_by(JobRun.id.desc()).limit(1)
        )
    assert run is not None and run.status == "succeeded"
    async with get_engine().connect() as other:  # the server released the lock with the session
        assert await other.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": TICK_LOCK})
        await other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": TICK_LOCK})
