"""Human decisions update existing derivations without discarding customer history."""

from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_stored_revalidation_sql import world

from app.db.models import Opportunity, OpportunitySignal, ReviewItem, User
from app.db.session import get_engine, get_sessionmaker
from app.pipeline.review import reconcile_reviewed_signal

DAY = date(2026, 9, 29)


@pytest.mark.parametrize("action", ["reject", "category"])
async def test_review_route_commits_decision_and_history_together(runtime, action):  # type: ignore[no-untyped-def]
    from app.api.routers.admin import decide_review
    from app.api.schemas import ReviewDecisionIn

    async with get_engine().connect() as connection:
        transaction = await connection.begin()
        try:
            async with AsyncSession(
                bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
            ) as session:
                data = await world(session, runtime)
                signal = data["signals"]["unsafe"]
                opportunity = data["opportunities"]["empty"]
                recommendation = data["recommendations"]["empty"]
                user = User(
                    org_id=recommendation.org_id,
                    email="reviewer-reconcile@example.test",
                    name="Reviewer",
                    password_hash="unused-in-route-test",
                    is_staff=True,
                )
                item = ReviewItem(signal_id=signal.id, reasons=["manual-check"])
                session.add_all([user, item])
                await session.flush()
                response = await decide_review(
                    item.id,
                    ReviewDecisionIn.model_validate(
                        {"action": "reject"}
                        if action == "reject"
                        else {"action": "edit", "category": "facility"}
                    ),
                    SimpleNamespace(user=user),
                    session,
                    runtime,
                )
                assert item.resolved_by == user.id
                assert item.resolution["previous_link"]["opportunity_id"] == opportunity.id
                if action == "reject":
                    assert response.status == "rejected"
                    assert opportunity.status == "dormant" and recommendation.score == 0
                    assert signal.verdict == "rejected"
                else:
                    assert response.status == "edited"
                    assert signal.category == opportunity.category == "facility"
                    assert item.resolution["changes"]["category"]["to"] == "facility"
                    assert recommendation.feedback == "relevant"
        finally:
            await transaction.rollback()


async def test_rejection_clears_summary_and_retains_feedback(runtime):  # type: ignore[no-untyped-def]
    async with get_sessionmaker()() as session:
        data = await world(session, runtime)
        signal = data["signals"]["unsafe"]
        opportunity = data["opportunities"]["empty"]
        recommendation = data["recommendations"]["empty"]
        signal.verdict = "rejected"
        ids = await reconcile_reviewed_signal(session, runtime, signal, today=DAY)
        assert opportunity.id in ids
        assert opportunity.status == "dormant"
        assert opportunity.signal_count == 0
        assert opportunity.conversion_prob == 0
        assert recommendation.score == 0
        assert recommendation.feedback == "relevant"
        assert recommendation.notified_stage == "council_mention"
        assert await session.get(Opportunity, opportunity.id) is opportunity
        assert (
            await session.scalar(
                select(OpportunitySignal).where(OpportunitySignal.signal_id == signal.id)
            )
            is None
        )
        assert await reconcile_reviewed_signal(session, runtime, signal, today=DAY) == []
        await session.rollback()


async def test_institution_edit_relinks_and_keeps_old_opportunity(runtime):  # type: ignore[no-untyped-def]
    async with get_sessionmaker()() as session:
        data = await world(session, runtime)
        signal = data["signals"]["unsafe"]
        original = data["opportunities"]["empty"]
        signal.institution_code = None
        await reconcile_reviewed_signal(session, runtime, signal, today=DAY)
        link = await session.scalar(
            select(OpportunitySignal).where(OpportunitySignal.signal_id == signal.id)
        )
        assert link is not None and link.opportunity_id != original.id
        new = await session.get(Opportunity, link.opportunity_id)
        assert new is not None and new.institution_code is None
        assert original.status == "dormant" and original.signal_count == 0
        # A retry keeps the same corrected identity and does not create another empty thread.
        await reconcile_reviewed_signal(session, runtime, signal, today=DAY)
        again = await session.scalar(
            select(OpportunitySignal).where(OpportunitySignal.signal_id == signal.id)
        )
        assert again is not None and again.opportunity_id == new.id
        await session.rollback()


async def test_manual_link_approval_keeps_identity_and_clears_tentative(runtime):  # type: ignore[no-untyped-def]
    async with get_sessionmaker()() as session:
        data = await world(session, runtime)
        signal = data["signals"]["manual"]
        opportunity = data["opportunities"]["manual"]
        link = await session.scalar(
            select(OpportunitySignal).where(OpportunitySignal.signal_id == signal.id)
        )
        link.method = "manual"
        link.tentative = True
        await reconcile_reviewed_signal(session, runtime, signal, today=DAY)
        assert link.opportunity_id == opportunity.id
        assert link.tentative is False
        assert opportunity.signal_count == 1
        review = await session.scalar(select(ReviewItem).where(ReviewItem.signal_id == signal.id))
        assert review.status == "approved"
        await session.rollback()
