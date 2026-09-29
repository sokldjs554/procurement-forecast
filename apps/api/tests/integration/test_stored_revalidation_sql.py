"""SQL-backed audit/apply checks: identity, human history, drift, and atomic failure."""

from datetime import date
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Brief,
    CompanyProfile,
    Document,
    DocumentChunk,
    Opportunity,
    OpportunitySignal,
    Organization,
    Recommendation,
    ReviewItem,
    Signal,
    Source,
)
from app.db.session import get_sessionmaker
from app.demo.seed import seed_institutions
from app.pipeline.revalidate import RevalidationPlanChangedError, revalidate_signals
from app.runtime import Runtime

DAY = date(2026, 9, 29)
SOURCE_KEY = "test_stored_revalidation"
MEMBER = "스마트쉘터를 설치해 주십시오."
OFFICIAL = "해당 사업은 내년부터 추진할 예정입니다."
TEXT = f"○위원 김민수  {MEMBER}\n○교통과장 이민수  {OFFICIAL}"


async def world(session: AsyncSession, runtime: Runtime) -> dict[str, object]:
    await seed_institutions(session, runtime)
    source = Source(key=SOURCE_KEY, name="Revalidation test", adapter="crawler", enabled=False)
    session.add(source)
    await session.flush()
    document = Document(
        source_id=source.id,
        external_id="revalidation-test",
        doc_type="council_minutes",
        title="회의록",
        published_at=DAY,
        content_hash="a" * 64,
        mime="text/plain",
        text=TEXT,
        parse_status="parsed",
        structured={},
    )
    session.add(document)
    await session.flush()
    chunk = DocumentChunk(
        document_id=document.id, seq=0, char_start=0, char_end=len(TEXT), text=TEXT
    )
    session.add(chunk)
    await session.flush()
    signals: dict[str, Signal] = {}
    for name, quote, verdict in (
        ("unsafe", MEMBER, "accepted"),
        ("mixed_unsafe", MEMBER, "accepted"),
        ("safe", OFFICIAL, "accepted"),
        ("manual", MEMBER, "accepted"),
        ("rejected", MEMBER, "rejected"),
        ("pending", MEMBER, "needs_review"),
    ):
        start = TEXT.index(quote)
        signal = Signal(
            document_id=document.id,
            chunk_id=chunk.id,
            stage="council_mention",
            institution_code="LG-41130",
            title=f"스마트쉘터 {name}",
            summary="test",
            category="smart_city",
            confidence=0.95,
            evidence=[{"quote": quote, "start": start, "end": start + len(quote)}],
            grounding={"legacy": "keep"},
            verdict=verdict,
            extractor="legacy-v1",
            observed_at=DAY,
            dedupe_key=f"revalidation-{name}",
            commitment="planned",
        )
        session.add(signal)
        signals[name] = signal
    await session.flush()
    review = ReviewItem(
        signal_id=signals["manual"].id,
        status="approved",
        reasons=["human-reviewed"],
        resolution={"action": "approve"},
    )
    session.add(review)
    opportunities: dict[str, Opportunity] = {}
    for name, members in (
        ("empty", ["unsafe"]),
        ("mixed", ["mixed_unsafe", "safe"]),
        ("manual", ["manual"]),
    ):
        opportunity = Opportunity(
            institution_code="LG-41130",
            title=name,
            category="smart_city",
            stage="council_mention",
            status="open",
            first_seen_at=DAY,
            last_signal_at=DAY,
            signal_count=len(members),
            conversion_prob=0.8,
        )
        session.add(opportunity)
        await session.flush()
        opportunities[name] = opportunity
        for member in members:
            session.add(
                OpportunitySignal(
                    opportunity_id=opportunity.id,
                    signal_id=signals[member].id,
                    score=1.0,
                    method="seed",
                    tentative=False,
                    reasons={"original": True},
                )
            )
    org = Organization(name="Revalidation customer")
    session.add(org)
    await session.flush()
    session.add(CompanyProfile(org_id=org.id, keywords=["스마트쉘터"]))
    recommendations = {}
    for name, opportunity in opportunities.items():
        recommendation = Recommendation(
            org_id=org.id,
            opportunity_id=opportunity.id,
            score=0.9,
            breakdown={},
            ranker_version="old",
            feedback="relevant",
            notified_stage="council_mention",
        )
        session.add(recommendation)
        recommendations[name] = recommendation
    brief = Brief(
        org_id=org.id,
        opportunity_id=opportunities["empty"].id,
        content_md="Keep historical report",
        model="test",
        credits_spent=0,
        idempotency_key="revalidation-brief",
    )
    session.add(brief)
    await session.flush()
    return {
        "signals": signals,
        "opportunities": opportunities,
        "recommendations": recommendations,
        "document": document,
        "review": review,
        "brief": brief,
    }


async def test_audit_then_apply_preserves_ids_and_human_history(runtime, monkeypatch):  # type: ignore[no-untyped-def]
    from app.pipeline import revalidate

    # Any future accidental model/embedding call must fail the regression.
    monkeypatch.setattr(type(runtime.llm), "extract", AsyncMock(side_effect=AssertionError("LLM")))
    monkeypatch.setattr(
        type(runtime.embedder), "embed", AsyncMock(side_effect=AssertionError("embed"))
    )
    async with get_sessionmaker()() as session:
        data = await world(session, runtime)
        signals = data["signals"]
        opportunities = data["opportunities"]
        recommendations = data["recommendations"]
        old_ids = [s.id for s in signals.values()]
        audit = await revalidate_signals(session, runtime, source_keys=[SOURCE_KEY], today=DAY)
        assert not audit.applied
        assert audit.counts == {
            "scanned": 4,
            "unchanged": 1,
            "needs_review": 2,
            "manual_review_required": 1,
            "links_removed": 2,
            "opportunities_refreshed": 2,
            "recommendations_refreshed": 2,
        }
        assert signals["unsafe"].verdict == "accepted"
        assert len(session.dirty) == 0
        applied = await revalidate_signals(
            session,
            runtime,
            source_keys=[SOURCE_KEY],
            today=DAY,
            apply=True,
            expected_digest=audit.digest,
        )
        assert applied.applied
        assert applied.digest == audit.digest
        assert signals["unsafe"].verdict == signals["mixed_unsafe"].verdict == "needs_review"
        assert signals["safe"].verdict == signals["manual"].verdict == "accepted"
        assert signals["rejected"].verdict == "rejected"
        assert signals["pending"].verdict == "needs_review"
        assert signals["unsafe"].grounding["legacy"] == "keep"
        assert signals["unsafe"].grounding["revalidation"]["removed_link"]["reasons"] == {
            "original": True
        }
        assert opportunities["empty"].status == "dormant"
        assert opportunities["empty"].signal_count == 0
        assert opportunities["mixed"].signal_count == 1
        assert opportunities["mixed"].title == signals["safe"].title
        assert recommendations["empty"].score == 0
        assert recommendations["mixed"].ranker_version == revalidate.RANKER_VERSION
        assert recommendations["manual"].score == 0.9
        assert all(r.feedback == "relevant" for r in recommendations.values())
        assert all(r.notified_stage == "council_mention" for r in recommendations.values())
        assert data["review"].status == "approved"
        assert data["review"].resolution == {"action": "approve"}
        assert await session.get(Brief, data["brief"].id) is not None
        assert (
            await session.scalar(
                select(func.count()).select_from(Signal).where(Signal.id.in_(old_ids))
            )
            == 6
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReviewItem)
                .where(ReviewItem.signal_id.in_(old_ids))
            )
            == 3
        )
        repeat = await revalidate_signals(session, runtime, source_keys=[SOURCE_KEY], today=DAY)
        assert repeat.counts["needs_review"] == 0
        await session.rollback()


async def test_digest_rejects_source_or_manual_decision_drift(runtime):  # type: ignore[no-untyped-def]
    async with get_sessionmaker()() as session:
        data = await world(session, runtime)
        audit = await revalidate_signals(session, runtime, source_keys=[SOURCE_KEY], today=DAY)
        data["document"].text += "\n추가된 원문"
        await session.flush()
        with pytest.raises(RevalidationPlanChangedError, match="changed"):
            await revalidate_signals(
                session,
                runtime,
                source_keys=[SOURCE_KEY],
                today=DAY,
                apply=True,
                expected_digest=audit.digest,
            )
        await session.refresh(data["signals"]["unsafe"])
        assert data["signals"]["unsafe"].verdict == "accepted"
        audit = await revalidate_signals(session, runtime, source_keys=[SOURCE_KEY], today=DAY)
        session.add(
            ReviewItem(signal_id=data["signals"]["unsafe"].id, status="approved", reasons=[])
        )
        await session.flush()
        with pytest.raises(RevalidationPlanChangedError):
            await revalidate_signals(
                session,
                runtime,
                source_keys=[SOURCE_KEY],
                today=DAY,
                apply=True,
                expected_digest=audit.digest,
            )
        await session.rollback()


async def test_apply_failure_rolls_back_whole_maintenance_savepoint(runtime, monkeypatch):  # type: ignore[no-untyped-def]
    from app.pipeline import revalidate

    async with get_sessionmaker()() as session:
        data = await world(session, runtime)
        unsafe_id = data["signals"]["unsafe"].id
        audit = await revalidate_signals(session, runtime, source_keys=[SOURCE_KEY], today=DAY)
        monkeypatch.setattr(
            revalidate, "refresh_opportunity", AsyncMock(side_effect=RuntimeError("refresh failed"))
        )
        with pytest.raises(RuntimeError, match="refresh failed"):
            await revalidate_signals(
                session,
                runtime,
                source_keys=[SOURCE_KEY],
                today=DAY,
                apply=True,
                expected_digest=audit.digest,
            )
        signal = await session.get(Signal, unsafe_id)
        assert signal.verdict == "accepted"
        assert (
            await session.scalar(
                select(OpportunitySignal).where(OpportunitySignal.signal_id == unsafe_id)
            )
            is not None
        )
        assert (
            await session.scalar(select(ReviewItem).where(ReviewItem.signal_id == unsafe_id))
            is None
        )
        await session.rollback()


async def test_pagination_and_explicit_apply_guard(runtime):  # type: ignore[no-untyped-def]
    async with get_sessionmaker()() as session:
        await world(session, runtime)
        first = await revalidate_signals(
            session, runtime, source_keys=[SOURCE_KEY], limit=1, today=DAY
        )
        assert first.counts["scanned"] == 1 and first.has_more
        second = await revalidate_signals(
            session,
            runtime,
            source_keys=[SOURCE_KEY],
            limit=10,
            after_id=first.next_after_id,
            today=DAY,
        )
        assert second.counts["scanned"] == 3 and not second.has_more
        with pytest.raises(ValueError, match="expected_digest"):
            await revalidate_signals(session, runtime, apply=True)
        await session.rollback()


async def test_manual_links_and_existing_open_review_are_preserved(runtime):  # type: ignore[no-untyped-def]
    async with get_sessionmaker()() as session:
        data = await world(session, runtime)
        unsafe_id = data["signals"]["unsafe"].id
        mixed_id = data["signals"]["mixed_unsafe"].id
        link = await session.scalar(
            select(OpportunitySignal).where(OpportunitySignal.signal_id == unsafe_id)
        )
        link.method = "manual"
        review = ReviewItem(signal_id=mixed_id, status="open", reasons=["prior-review-reason"])
        session.add(review)
        await session.flush()
        review_id = review.id
        audit = await revalidate_signals(session, runtime, source_keys=[SOURCE_KEY], today=DAY)
        assert audit.counts["manual_review_required"] == 2
        assert audit.counts["needs_review"] == 1
        await revalidate_signals(
            session,
            runtime,
            source_keys=[SOURCE_KEY],
            today=DAY,
            apply=True,
            expected_digest=audit.digest,
        )
        assert data["signals"]["unsafe"].verdict == "accepted"
        assert link.method == "manual"
        assert review.id == review_id
        assert "prior-review-reason" in review.reasons
        assert "official_evidence_missing" in review.reasons
        await session.rollback()
