"""Real PostgreSQL isolation, graph closure and fail-closed snapshot publication."""

import asyncio
import hashlib
import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import delete, select, update

from app.clock import now_utc
from app.db.models import Document, InstitutionRow, Opportunity, OpportunitySignal, Signal, Source
from app.db.session import get_sessionmaker
from app.eval.forecast_snapshot import SnapshotLimitError, export_forecast_snapshot
from app.settings import Settings

CODE = "LG-SNAPSHOT-TEST"
OTHER = "LG-SNAPSHOT-OTHER"
TEXT = "○교통과장 이민수  스마트쉘터를 설치할 계획입니다."


@pytest.fixture
async def snapshot_world(migrated_db):  # type: ignore[no-untyped-def]
    async with get_sessionmaker()() as session:
        for code in (CODE, OTHER):
            session.add(
                InstitutionRow(
                    code=code,
                    name="Snapshot test",
                    kind="local_gov",
                    sido="경기도",
                    region_code="41130",
                )
            )
        source = Source(key="snapshot-test-sql", name="Snapshot", adapter="fixture", enabled=False)
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            external_id="snapshot-test",
            doc_type="council_minutes",
            title="Snapshot test",
            published_at=date(2025, 1, 1),
            mime="text/plain",
            content_hash=hashlib.sha256(TEXT.encode()).hexdigest(),
            text=TEXT,
            parse_status="parsed",
            structured={"published_from": "meeting_date"},
        )
        opportunity = Opportunity(
            institution_code=CODE,
            title="Snapshot opportunity",
            category="smart_city",
            stage="council_mention",
            status="open",
            first_seen_at=date(2025, 1, 1),
            last_signal_at=date(2025, 1, 1),
            conversion_prob=0.42,
            signal_count=2,
        )
        session.add_all([document, opportunity])
        await session.flush()
        for index, code in enumerate((CODE, OTHER)):
            signal = Signal(
                document_id=document.id,
                institution_code=code,
                stage="council_mention",
                title="스마트쉘터 설치",
                summary="설치",
                category="smart_city",
                confidence=0.9,
                evidence=[{"quote": TEXT, "start": 0, "end": len(TEXT), "found": True}],
                grounding={},
                verdict="accepted",
                extractor="heuristic-v1",
                observed_at=date(2025, 1, 1),
                embedding=[1.0] + [0.0] * 511,
                dedupe_key=f"snapshot-test-{index}",
            )
            session.add(signal)
            await session.flush()
            session.add(
                OpportunitySignal(
                    opportunity_id=opportunity.id,
                    signal_id=signal.id,
                    score=1.0,
                    method="seed",
                    tentative=False,
                    reasons={},
                )
            )
        await session.commit()
        ids = {"source": source.id, "document": document.id, "opportunity": opportunity.id}
    try:
        yield ids
    finally:
        async with get_sessionmaker()() as session:
            await session.execute(
                delete(OpportunitySignal).where(
                    OpportunitySignal.opportunity_id == ids["opportunity"]
                )
            )
            await session.execute(delete(Signal).where(Signal.document_id == ids["document"]))
            await session.execute(delete(Opportunity).where(Opportunity.id == ids["opportunity"]))
            await session.execute(delete(Document).where(Document.id == ids["document"]))
            await session.execute(delete(Source).where(Source.id == ids["source"]))
            await session.execute(
                delete(InstitutionRow).where(InstitutionRow.code.in_([CODE, OTHER]))
            )
            await session.commit()


async def test_graph_cap_is_fail_closed_and_current_state_is_frozen(snapshot_world, tmp_path: Path):  # type: ignore[no-untyped-def]
    target = tmp_path / "snapshot.jsonl"
    target.write_text("keep existing snapshot")
    async with get_sessionmaker()() as session:
        with pytest.raises(SnapshotLimitError):
            await export_forecast_snapshot(
                session, target, institution_code=CODE, code_revision="abc1234", max_signals=1
            )
    assert target.read_text() == "keep existing snapshot"
    before = now_utc()
    async with get_sessionmaker()() as session:
        report = await export_forecast_snapshot(
            session,
            target,
            institution_code=CODE,
            code_revision="abc1234",
            settings=Settings(env="test"),
        )
        assert not session.in_transaction() and not session.dirty
    assert report.counts["signal"] == 2
    assert report.counts["link"] == 2
    assert report.incomplete_reasons["cross_institution_membership_signal"] == 1
    rows = [json.loads(line) for line in target.read_text().splitlines()]
    assert rows[0]["captured_at"] >= before.isoformat()
    assert rows[0]["historical_as_of_verified"] is False
    prediction = next(row for row in rows if row["record_type"] == "prediction")
    assert prediction["data"]["conversion_prob"] == 0.42
    assert next(row for row in rows if row.get("entity") == "document")["data"]["text"] == TEXT
    assert (
        len(
            next(row for row in rows if row.get("entity") == "signal")["data"]["embedding"][
                "values"
            ]
        )
        == 512
    )


async def test_repeatable_read_resists_concurrent_source_and_prediction_update(
    snapshot_world, tmp_path: Path, monkeypatch
):  # type: ignore[no-untyped-def]
    from app.eval import forecast_snapshot

    original = forecast_snapshot._read_graph
    established, changed = asyncio.Event(), asyncio.Event()

    async def pause_after_snapshot(session, institution_code, max_signals):  # type: ignore[no-untyped-def]
        await session.scalar(select(InstitutionRow.code).where(InstitutionRow.code == CODE))
        established.set()
        await asyncio.wait_for(changed.wait(), timeout=10)
        return await original(session, institution_code, max_signals)

    monkeypatch.setattr(forecast_snapshot, "_read_graph", pause_after_snapshot)

    async def concurrent_update():  # type: ignore[no-untyped-def]
        await asyncio.wait_for(established.wait(), timeout=10)
        async with get_sessionmaker()() as session:
            await session.execute(
                update(Document)
                .where(Document.id == snapshot_world["document"])
                .values(text="changed source")
            )
            await session.execute(
                update(Opportunity)
                .where(Opportunity.id == snapshot_world["opportunity"])
                .values(conversion_prob=0.99)
            )
            await session.commit()
        changed.set()

    writer = asyncio.create_task(concurrent_update())
    async with get_sessionmaker()() as session:
        await export_forecast_snapshot(
            session,
            tmp_path / "consistent.jsonl",
            institution_code=CODE,
            code_revision="abc1234",
            settings=Settings(env="test"),
        )
    await writer
    rows = [json.loads(line) for line in (tmp_path / "consistent.jsonl").read_text().splitlines()]
    assert next(row for row in rows if row.get("entity") == "document")["data"]["text"] == TEXT
    assert (
        next(row for row in rows if row["record_type"] == "prediction")["data"]["conversion_prob"]
        == 0.42
    )
    async with get_sessionmaker()() as session:
        assert (
            await session.scalar(
                select(Document.text).where(Document.id == snapshot_world["document"])
            )
            == "changed source"
        )
