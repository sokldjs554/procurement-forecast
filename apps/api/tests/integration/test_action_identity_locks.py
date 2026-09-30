"""Customer action snapshots hold the identity lock used by automatic link writers."""

import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal
from app.api.routers.opportunities import feedback
from app.api.schemas import FeedbackIn
from app.db.models import (
    AlertChannel,
    AlertRule,
    CompanyProfile,
    Document,
    InstitutionRow,
    LinkReconciliationState,
    Notification,
    Opportunity,
    OpportunitySignal,
    Organization,
    Recommendation,
    Signal,
    Source,
    User,
)
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.notify import dispatch
from app.pipeline import brief, relations


async def add_signal(session, opportunity, *, stage="budget_line", reference=True):
    source = Source(
        key=f"lock-source-{uuid4().hex}", name="Test evidence", adapter="fixture", enabled=False
    )
    session.add(source)
    await session.flush()
    doc = Document(
        source_id=source.id,
        external_id=f"evidence-{uuid4().hex}",
        doc_type="bid_notice" if stage == "bid_notice" else "budget_book",
        title=opportunity.title,
        institution_code=opportunity.institution_code,
        published_at=date(2026, 9, 29),
        content_hash="a" * 64,
        mime="text/plain",
        text=opportunity.title,
        parse_status="parsed",
    )
    session.add(doc)
    await session.flush()
    row = Signal(
        document_id=doc.id,
        institution_code=opportunity.institution_code,
        title=opportunity.title,
        summary="Original grounded evidence",
        category="facility",
        stage=stage,
        observed_at=date(2026, 9, 29),
        verdict="accepted",
        budget_krw=100_000_000,
        commitment="committed",
        confidence=0.99,
        expected_year=2027,
        expected_half="H1",
        extractor="fixture",
        dedupe_key=uuid4().hex,
        external_refs={"order_plan_no": "ANCHOR-LIFECYCLE"} if reference else {},
        embedding=[1.0] + [0.0] * 511,
    )
    session.add(row)
    await session.flush()
    return row, source.id


@pytest.fixture
async def action_world(runtime):
    code = f"LOCK-{uuid4().hex[:20]}"
    async with session_scope() as session:
        session.add(
            InstitutionRow(
                code=code,
                name="Lock test institution",
                kind="local_gov",
                sido="경기도",
                region_code="41130",
            )
        )
        await session.flush()
        org = Organization(name="Identity lock test", plan="pro", credit_balance=10)
        session.add(org)
        await session.flush()
        user = User(
            org_id=org.id,
            email=f"lock-{uuid4()}@example.test",
            name="Reviewer",
            password_hash="unused",
            is_staff=True,
        )
        opp = Opportunity(
            institution_code=code,
            title="Protected report identity",
            category="facility",
            stage="budget_line",
            status="open",
            first_seen_at=date(2026, 9, 29),
            last_signal_at=date(2026, 9, 29),
        )
        session.add_all([user, opp])
        await session.flush()
        original, source_id = await add_signal(session, opp)
        session.add(
            OpportunitySignal(
                opportunity_id=opp.id,
                signal_id=original.id,
                score=1.0,
                method="seed",
                tentative=False,
                reasons={},
            )
        )
        await session.flush()
        from app.pipeline.link import refresh_opportunity

        await refresh_opportunity(session, opp, today=date(2026, 9, 29))
        session.add_all(
            [
                CompanyProfile(
                    org_id=org.id,
                    keywords=["Protected", "identity"],
                    categories=["facility"],
                    region_codes=["41130"],
                    budget_min=10_000_000,
                    budget_max=200_000_000,
                    embedding=[1.0] + [0.0] * 511,
                ),
                Recommendation(
                    org_id=org.id,
                    opportunity_id=opp.id,
                    score=0.9,
                    breakdown={},
                    ranker_version="test",
                ),
                AlertRule(org_id=org.id, mode="instant", min_score=0.5),
                AlertChannel(
                    org_id=org.id, kind="email", target="unused@example.test", enabled=True
                ),
            ]
        )
        ids = org.id, user.id, opp.id
    try:
        yield ids
    finally:
        async with session_scope() as session:
            await session.execute(delete(Organization).where(Organization.id == ids[0]))
            await session.execute(
                delete(LinkReconciliationState).where(
                    LinkReconciliationState.institution_code == code
                )
            )
            from app.db.models import OpportunityCustomerAnchor

            await session.execute(
                delete(OpportunityCustomerAnchor).where(
                    OpportunityCustomerAnchor.opportunity_id == ids[2]
                )
            )
            await session.execute(delete(Document).where(Document.source_id == source_id))
            await session.execute(delete(Source).where(Source.id == source_id))
            await session.execute(delete(Opportunity).where(Opportunity.id == ids[2]))
            await session.execute(delete(InstitutionRow).where(InstitutionRow.code == code))


async def assert_identity_locked(opportunity_id):
    """A separate SQL transaction must fail immediately, not change the action's identity."""
    async with get_sessionmaker()() as contender:
        with pytest.raises(DBAPIError) as error:
            await contender.execute(
                select(Opportunity.id)
                .where(Opportunity.id == opportunity_id)
                .with_for_update(nowait=True)
            )
        assert getattr(error.value.orig, "sqlstate", None) == "55P03"
        await contender.rollback()


async def test_feedback_locks_identity_before_publishing_customer_anchor(action_world):
    org_id, user_id, opportunity_id = action_world
    async with get_sessionmaker()() as session:
        principal = Principal(
            await session.get(User, user_id), await session.get(Organization, org_id)
        )
        await feedback(opportunity_id, FeedbackIn(feedback="relevant"), principal, session)
        # No flush: a Recommendation UPDATE/FK lock must not accidentally make this pass.
        await assert_identity_locked(opportunity_id)
        await session.rollback()


@pytest.mark.parametrize("actor", ["brief", "notification", "relation"])
async def test_action_locks_identity_before_reading_or_generating_snapshot(
    action_world, monkeypatch, actor
):
    org_id, user_id, opportunity_id = action_world
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause():
        entered.set()
        await release.wait()

    async with get_sessionmaker()() as session:
        org = await session.get(Organization, org_id)
        user = await session.get(User, user_id)
        if actor == "brief":

            async def fixture_brief(current_session, facts):
                assert facts.title == "Protected report identity"
                await pause()
                return "Fixture report, no external request", "fixture"

            operation = brief.generate_brief(
                session,
                SimpleNamespace(llm=SimpleNamespace(brief=fixture_brief)),
                org=org,
                opportunity_id=opportunity_id,
                user_id=user_id,
                idempotency_key=f"identity-lock-{uuid4()}",
            )
        elif actor == "notification":
            original = dispatch.build_items

            async def paused_items(*args, **kwargs):
                await pause()
                return await original(*args, **kwargs)

            monkeypatch.setattr(dispatch, "build_items", paused_items)
            operation = dispatch.enqueue_alerts(session, org_id, web_url="https://example.test")
        else:
            original = relations._endpoints

            async def paused_endpoints(*args, **kwargs):
                await pause()
                return await original(*args, **kwargs)

            monkeypatch.setattr(relations, "_endpoints", paused_endpoints)
            # Pause before evidence validation; no fabricated relation is persisted.
            operation = relations.decide_relation(
                session,
                project_id=opportunity_id,
                contract_id=opportunity_id + 10**9,
                status="proposed",
                expected_version=0,
                evidence_signal_ids=[],
                note="lock test",
                actor=user,
                idempotency_key=f"identity-lock-{uuid4()}",
            )
        task = asyncio.create_task(operation)
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            await assert_identity_locked(opportunity_id)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await session.rollback()


async def test_notification_rechecks_dirty_generation_after_candidate_identity_lock(action_world):
    org_id, _, opportunity_id = action_world
    read, changed = asyncio.Event(), asyncio.Event()

    class PausedCandidateSession(AsyncSession):
        paused = False

        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            if not self.paused and any(
                item.get("entity") is Recommendation
                for item in getattr(statement, "column_descriptions", [])
            ):
                self.paused = True
                read.set()
                await changed.wait()
            return result

    async with PausedCandidateSession(bind=get_engine(), expire_on_commit=False) as session:
        task = asyncio.create_task(
            dispatch.enqueue_alerts(session, org_id, web_url="https://example.test")
        )
        try:
            await asyncio.wait_for(read.wait(), timeout=5)
            async with session_scope() as writer:
                opp = await writer.get(Opportunity, opportunity_id, with_for_update=True)
                writer.add(LinkReconciliationState(institution_code=opp.institution_code))
            changed.set()
            assert await asyncio.wait_for(task, timeout=5) == 0
            assert (
                await session.scalar(select(Notification.id).where(Notification.org_id == org_id))
                is None
            )
            rec = await session.get(Recommendation, (org_id, opportunity_id))
            assert rec.notified_stage is None
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            await session.rollback()


@pytest.mark.parametrize("actor", ["feedback", "brief", "notification"])
async def test_first_customer_action_captures_core_without_replacing_it(action_world, actor):
    from app.db.models import OpportunityCustomerAnchor
    from app.pipeline.link_state import capture_customer_anchor
    from app.pipeline.process import ReprocessingProtectedError, protect_human_decisions

    org_id, user_id, opportunity_id = action_world
    async with get_sessionmaker()() as session:
        opp = await session.get(Opportunity, opportunity_id)
        original_ids = list(
            await session.scalars(
                select(OpportunitySignal.signal_id).where(
                    OpportunitySignal.opportunity_id == opportunity_id
                )
            )
        )
        org = await session.get(Organization, org_id)
        user = await session.get(User, user_id)

        async def fixture_brief(current_session, facts):
            return "Immutable fixture brief", "fixture"

        async def publish(suffix):
            if actor == "feedback":
                await feedback(
                    opportunity_id, FeedbackIn(feedback="relevant"), Principal(user, org), session
                )
            elif actor == "brief":
                await brief.generate_brief(
                    session,
                    SimpleNamespace(llm=SimpleNamespace(brief=fixture_brief)),
                    org=org,
                    opportunity_id=opportunity_id,
                    user_id=user_id,
                    idempotency_key=f"anchor-{uuid4()}-{suffix}",
                )
            else:
                await dispatch.enqueue_alerts(session, org_id, web_url="https://example.test")

        await publish("first")
        anchor = await session.get(
            OpportunityCustomerAnchor, opportunity_id, populate_existing=True
        )
        assert anchor is not None and anchor.signal_ids == original_ids
        original = await session.get(Signal, original_ids[0])
        with pytest.raises(ReprocessingProtectedError, match="published customer evidence"):
            await protect_human_decisions(session, original.document_id)
        # Simulate upgraded history whose immutable core has not yet been captured.
        await session.execute(
            delete(OpportunityCustomerAnchor).where(
                OpportunityCustomerAnchor.opportunity_id == opportunity_id
            )
        )
        with pytest.raises(ReprocessingProtectedError, match="published customer evidence"):
            await protect_human_decisions(session, original.document_id)
        anchor = await capture_customer_anchor(session, opportunity_id)
        assert anchor.signal_ids == original_ids
        later, _ = await add_signal(session, opp, stage="bid_notice")
        session.add(
            OpportunitySignal(
                opportunity_id=opportunity_id,
                signal_id=later.id,
                score=1.0,
                method="ref",
                tentative=False,
                reasons={},
            )
        )
        await session.flush()
        from app.pipeline.link import refresh_opportunity

        await refresh_opportunity(session, opp, today=date(2026, 9, 29))
        await publish("second")
        await session.refresh(anchor)
        assert anchor.signal_ids == original_ids
        assert later.id not in anchor.signal_ids
        # Only the first core is immutable; re-extraction of a later automatic document
        # must remain possible even after subsequent customer actions publish it.
        await protect_human_decisions(session, later.document_id)
        await session.rollback()


@pytest.mark.parametrize("reference", [False, True])
async def test_notified_budget_identity_accepts_later_bid_without_changing_original_core(
    action_world, runtime, reference
):
    from app.db.models import OpportunityCustomerAnchor
    from app.pipeline.link import link_signals
    from app.pipeline.link_reconcile import reconcile_institution

    org_id, _, opportunity_id = action_world
    async with get_sessionmaker()() as session:
        opp = await session.get(Opportunity, opportunity_id)
        original = await session.scalar(
            select(Signal)
            .join(OpportunitySignal)
            .where(OpportunitySignal.opportunity_id == opportunity_id)
        )
        if not reference:
            original.external_refs = {}
            await session.flush()
        assert await dispatch.enqueue_alerts(session, org_id, web_url="https://example.test") == 1
        original_notification = await session.scalar(
            select(Notification).where(Notification.org_id == org_id)
        )
        original_payload = dict(original_notification.payload)
        bid, _ = await add_signal(session, opp, stage="bid_notice", reference=reference)
        await link_signals(session, runtime, [bid.id], today=date(2026, 9, 29))
        await reconcile_institution(session, runtime, opp.institution_code, today=date(2026, 9, 29))
        await session.refresh(opp)
        anchor = await session.get(
            OpportunityCustomerAnchor, opportunity_id, populate_existing=True
        )
        assert anchor.signal_ids == [original.id]
        assert (
            await session.scalar(
                select(OpportunitySignal.opportunity_id).where(
                    OpportunitySignal.signal_id == bid.id
                )
            )
            == opportunity_id
        )
        assert opp.stage == "bid_notice" and opp.signal_count == 2
        await session.refresh(original_notification)
        assert original_notification.payload == original_payload
        assert (
            await dispatch.enqueue_alerts(
                session,
                org_id,
                web_url="https://example.test",
                now=datetime(2026, 9, 29, tzinfo=UTC),
            )
            == 1
        )
        await session.refresh(anchor)
        assert anchor.signal_ids == [original.id]
        await session.rollback()
