"""Re-extraction must never erase existing human review or manual link decisions."""

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import now_utc
from app.db.models import (
    Document,
    DocumentChunk,
    Opportunity,
    OpportunitySignal,
    Organization,
    Recommendation,
    ReviewItem,
    Signal,
    Source,
    User,
)
from app.db.session import get_sessionmaker
from app.domain.institutions import Resolution
from app.pipeline.ingest import reresolve_institutions, upsert_record
from app.pipeline.link import link_signals
from app.pipeline.process import ReprocessingProtectedError, process_document
from app.runtime import Runtime
from app.sources.base import RawRecord


async def test_protected_document_stops_before_extraction_without_database() -> None:
    # The database boundary is unavailable in unit environments; the integration cases below
    # exercise the real joins and cascading rows. This catches a missing process entry guard.
    session = AsyncMock(spec=AsyncSession)
    session.get.return_value = Document(
        id=123, text="", structured={}, doc_type="council_minutes", parse_status="parsed"
    )
    session.scalars.return_value = [456]
    session.scalar.return_value = 789
    with pytest.raises(ReprocessingProtectedError, match="human decisions"):
        await process_document(session, AsyncMock(spec=Runtime), 123)


async def _document(session: AsyncSession, runtime: Runtime, key: str) -> Document:
    source = Source(key=key, name=key, adapter="g2b", enabled=False)
    session.add(source)
    await session.flush()
    doc, _ = await upsert_record(
        session,
        source,
        RawRecord(
            external_id=key,
            doc_type="order_plan",
            title="스마트쉘터 설치 사업",
            published_at=date(2026, 9, 1),
            mime="text/plain",
            publisher_raw="경기도 성남시",
            institution_code_hint="LG-41130",
            content="스마트쉘터 설치 사업 3억 원".encode(),
            structured={"amount_krw": 300_000_000, "order_year": 2027},
        ),
        runtime,
    )
    result = await process_document(session, runtime, doc.id)
    assert len(result.signal_ids) == 1
    await link_signals(session, runtime, result.signal_ids, today=date(2026, 9, 29))
    return doc


@pytest.mark.parametrize(
    "protection",
    ["approved", "edited", "rejected", "resolution", "resolved_at", "resolved_by", "manual"],
)
async def test_reprocessing_preserves_human_decision_rows(
    demo_world: Any, runtime: Runtime, protection: str
) -> None:
    async with get_sessionmaker()() as session:
        doc = await _document(session, runtime, f"reprocess_guard_{protection}")
        signal = await session.scalar(select(Signal).where(Signal.document_id == doc.id))
        assert signal is not None
        link = await session.scalar(
            select(OpportunitySignal).where(OpportunitySignal.signal_id == signal.id)
        )
        assert link is not None
        if protection == "manual":
            link.method = "manual"
        else:
            reviewer_id = await session.scalar(select(User.id).order_by(User.id).limit(1))
            assert reviewer_id is not None
            session.add(
                ReviewItem(
                    signal_id=signal.id,
                    reasons=["test_review"],
                    status=protection
                    if protection in {"approved", "edited", "rejected"}
                    else "open",
                    resolution={"action": "approve"} if protection == "resolution" else {},
                    resolved_at=now_utc() if protection == "resolved_at" else None,
                    resolved_by=reviewer_id if protection == "resolved_by" else None,
                )
            )
        await session.flush()
        ids_before = list(
            await session.scalars(
                select(DocumentChunk.id).where(DocumentChunk.document_id == doc.id)
            )
        )
        state_before = (doc.parse_status, doc.parse_error, doc.extracted_at)

        with pytest.raises(ReprocessingProtectedError, match="human decisions"):
            await process_document(session, runtime, doc.id)

        assert (doc.parse_status, doc.parse_error, doc.extracted_at) == state_before
        assert (
            await session.scalar(select(Signal.id).where(Signal.document_id == doc.id)) == signal.id
        )
        assert (
            list(
                await session.scalars(
                    select(DocumentChunk.id).where(DocumentChunk.document_id == doc.id)
                )
            )
            == ids_before
        )
        assert (
            await session.scalar(
                select(OpportunitySignal.method).where(OpportunitySignal.signal_id == signal.id)
            )
            == link.method
        )
        if protection != "manual":
            review = await session.scalar(
                select(ReviewItem).where(ReviewItem.signal_id == signal.id)
            )
            assert review is not None
            assert review.status == (
                protection if protection in {"approved", "edited", "rejected"} else "open"
            )
        await session.rollback()


async def test_unresolved_machine_review_still_allows_reprocessing(
    demo_world: Any, runtime: Runtime
) -> None:
    async with get_sessionmaker()() as session:
        doc = await _document(session, runtime, "reprocess_open_review")
        old_signal_id = await session.scalar(select(Signal.id).where(Signal.document_id == doc.id))
        assert old_signal_id is not None
        session.add(
            ReviewItem(signal_id=old_signal_id, status="open", reasons=["automatic_review"])
        )
        await session.flush()
        result = await process_document(session, runtime, doc.id)
        assert len(result.signal_ids) == 1
        assert result.signal_ids != [old_signal_id]
        assert await session.scalar(select(Signal.id).where(Signal.id == old_signal_id)) is None
        assert doc.parse_status == "parsed"
        await session.rollback()


async def test_machine_reprocessing_retracts_old_summary_without_database() -> None:
    opportunity = Opportunity(id=9, status="open", signal_count=1, conversion_prob=0.8)
    recommendation = Recommendation(
        org_id=2,
        opportunity_id=9,
        score=0.9,
        feedback="relevant",
        notified_stage="order_plan",
        breakdown={},
    )
    session = AsyncMock(spec=AsyncSession)
    session.get.return_value = Document(
        id=123, text="", structured={}, doc_type="council_minutes", parse_status="parsed"
    )
    session.scalar.return_value = None

    async def scalars(statement: Any) -> Any:
        selected = statement.column_descriptions[0]
        entity = selected["entity"]
        rows: list[Any] = []
        if entity is Signal and selected["name"] == "id":
            rows = [456]
        elif entity is OpportunitySignal:
            rows = [9]
        elif entity is Opportunity:
            rows = [opportunity]
        elif entity is Recommendation:
            rows = [recommendation]
        result = MagicMock()
        result.__iter__.return_value = iter(rows)
        result.all.return_value = rows
        return result

    session.scalars.side_effect = scalars
    session.execute.return_value = MagicMock()
    session.execute.return_value.all.return_value = []
    await process_document(session, AsyncMock(spec=Runtime), 123)
    assert opportunity.status == "dormant"
    assert opportunity.signal_count == 0
    assert recommendation.score == 0
    assert recommendation.feedback == "relevant"
    assert recommendation.notified_stage == "order_plan"


async def test_legacy_customer_history_blocks_reprocessing_before_first_core_capture(
    demo_world: Any, runtime: Runtime
) -> None:
    async with get_sessionmaker()() as session:
        doc = await _document(session, runtime, "reprocess_old_aggregate")
        old_signal = await session.scalar(select(Signal).where(Signal.document_id == doc.id))
        assert old_signal is not None
        link = await session.scalar(
            select(OpportunitySignal).where(OpportunitySignal.signal_id == old_signal.id)
        )
        assert link is not None
        opportunity = Opportunity(
            institution_code=old_signal.institution_code,
            title=old_signal.title,
            category=old_signal.category,
            stage="order_plan",
            status="open",
            first_seen_at=date(2026, 9, 1),
            last_signal_at=date(2026, 9, 1),
            signal_count=1,
            est_budget_krw=300_000_000,
            best_commitment="committed",
            conversion_prob=0.8,
        )
        organization = Organization(name="Reprocessing history test")
        session.add_all([opportunity, organization])
        await session.flush()
        link.opportunity_id = opportunity.id
        recommendation = Recommendation(
            org_id=organization.id,
            opportunity_id=opportunity.id,
            score=0.9,
            ranker_version="original",
            breakdown={},
            feedback="relevant",
            feedback_at=now_utc(),
            notified_stage="order_plan",
        )
        session.add(recommendation)
        await session.flush()
        history = (
            recommendation.feedback,
            recommendation.feedback_at,
            recommendation.notified_stage,
        )

        # This history predates the core table. It must protect original evidence even
        # when reprocessing happens before the first reconciliation/automatic append.
        with pytest.raises(ReprocessingProtectedError, match="published customer evidence"):
            await process_document(session, runtime, doc.id)

        assert await session.get(Signal, old_signal.id) is old_signal
        assert link.opportunity_id == opportunity.id
        assert opportunity.status == "open"
        assert opportunity.signal_count == 1
        assert opportunity.est_budget_krw == 300_000_000
        assert opportunity.best_commitment == "committed"
        assert opportunity.conversion_prob == 0.8
        assert recommendation.score == 0.9
        assert (
            recommendation.feedback,
            recommendation.feedback_at,
            recommendation.notified_stage,
        ) == history
        assert await session.get(Opportunity, opportunity.id) is opportunity
        assert (
            await session.get(Recommendation, (organization.id, opportunity.id)) is recommendation
        )
        await session.rollback()


async def test_changed_protected_source_is_refused_before_overwrite_without_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock(spec=AsyncSession)
    existing = Document(
        id=123,
        content_hash="old-hash",
        text="reviewed source text",
        raw_uri="original://raw",
        title="reviewed title",
        parse_status="parsed",
        structured={"reviewed": True},
    )
    session.scalar.side_effect = [existing, 789]
    session.scalars.return_value = [456]
    monkeypatch.setattr(
        "app.pipeline.ingest.resolve_institution",
        AsyncMock(return_value=Resolution(None, None, 0, "none")),
    )
    monkeypatch.setattr("app.pipeline.ingest.store_raw", AsyncMock(return_value="changed://raw"))
    changed = RawRecord(
        external_id="protected",
        doc_type="order_plan",
        title="changed title",
        published_at=date(2026, 9, 29),
        mime="text/plain",
        content=b"changed content",
    )
    with pytest.raises(ReprocessingProtectedError, match="human decisions"):
        await upsert_record(
            session, Source(id=1, key="protected"), changed, AsyncMock(spec=Runtime)
        )
    assert existing.text == "reviewed source text"
    assert existing.raw_uri == "original://raw"
    assert existing.title == "reviewed title"
    assert existing.structured == {"reviewed": True}
    assert existing.parse_status == "parsed"


async def test_changed_record_preserves_reviewed_source_and_same_hash_still_skips(
    demo_world: Any, runtime: Runtime
) -> None:
    async with get_sessionmaker()() as session:
        doc = await _document(session, runtime, "ingest_reviewed_source")
        source = await session.get(Source, doc.source_id)
        assert source is not None
        signal_id = await session.scalar(select(Signal.id).where(Signal.document_id == doc.id))
        assert signal_id is not None
        session.add(ReviewItem(signal_id=signal_id, status="approved", reasons=["human"]))
        await session.flush()
        original = (
            doc.title,
            doc.text,
            doc.raw_uri,
            doc.content_hash,
            dict(doc.structured),
            doc.parse_status,
        )
        changed = RawRecord(
            external_id=doc.external_id,
            doc_type="order_plan",
            title="수정된 스마트쉘터 설치 사업",
            published_at=date(2026, 9, 2),
            mime="text/plain",
            content="스마트쉘터 설치 사업 취소".encode(),
        )
        with pytest.raises(ReprocessingProtectedError, match="human decisions"):
            await upsert_record(session, source, changed, runtime)
        assert (
            doc.title,
            doc.text,
            doc.raw_uri,
            doc.content_hash,
            doc.structured,
            doc.parse_status,
        ) == original
        unchanged = RawRecord(
            external_id=doc.external_id,
            doc_type="order_plan",
            title=doc.title,
            published_at=doc.published_at,
            mime="text/plain",
            content="스마트쉘터 설치 사업 3억 원".encode(),
            structured={"amount_krw": 300_000_000, "order_year": 2027},
        )
        same, action = await upsert_record(session, source, unchanged, runtime)
        assert same.id == doc.id and action == "skipped"
        assert same.text == original[1]
        await session.rollback()


async def test_reresolve_skips_protected_document_with_explicit_count(
    demo_world: Any, runtime: Runtime
) -> None:
    async with get_sessionmaker()() as session:
        doc = await _document(session, runtime, "reresolve_reviewed_source")
        signal_id = await session.scalar(select(Signal.id).where(Signal.document_id == doc.id))
        assert signal_id is not None
        doc.institution_code = None
        session.add(ReviewItem(signal_id=signal_id, status="approved", reasons=["human"]))
        await session.flush()
        original = (doc.text, dict(doc.structured), doc.parse_status)
        report = await reresolve_institutions(session, runtime)
        assert report["protected"] >= 1  # type: ignore[operator]
        await session.refresh(doc)
        assert doc.institution_code is None
        assert (doc.text, doc.structured, doc.parse_status) == original
        await session.rollback()
