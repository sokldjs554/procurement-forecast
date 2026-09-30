"""Chosen-target locks serialize provisional linking with customer-history writers."""

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from app.db.models import (
    Brief,
    Document,
    InstitutionRow,
    Opportunity,
    OpportunitySignal,
    Organization,
    ReviewItem,
    Signal,
    Source,
)
from app.db.session import get_sessionmaker
from app.pipeline import link

TODAY = date(2026, 9, 29)
RUNTIME = SimpleNamespace(settings=SimpleNamespace(link_threshold=0.6, link_review_band=0.08))


async def world(session):
    key = uuid4().hex
    code = f"LOCK-{key[:20]}"
    source = Source(key=f"link-lock-{key}", name="lock test", adapter="fixture", enabled=False)
    org = Organization(name=f"link lock {key}")
    session.add_all(
        [
            source,
            org,
            InstitutionRow(code=code, name=code, kind="local_gov", sido="서울", region_code="11"),
        ]
    )
    await session.flush()
    opp = Opportunity(
        institution_code=code,
        title="청사 냉난방기 교체",
        category="facility",
        stage="order_plan",
        status="open",
        first_seen_at=TODAY,
        last_signal_at=TODAY,
        signal_count=1,
    )
    session.add(opp)
    await session.flush()
    signals = []
    for number in range(2):
        doc = Document(
            source_id=source.id,
            external_id=f"{key}-{number}",
            doc_type="order_plan",
            title=opp.title,
            content_hash=key,
            mime="application/json",
            published_at=TODAY,
            institution_code=code,
        )
        session.add(doc)
        await session.flush()
        signal = Signal(
            document_id=doc.id,
            institution_code=code,
            stage="order_plan",
            title=opp.title,
            category="facility",
            observed_at=TODAY,
            verdict="accepted",
            extractor="test",
            dedupe_key=f"{key}-{number}",
            external_refs={"order_plan_no": key},
            keywords=[],
        )
        session.add(signal)
        await session.flush()
        signals.append(signal)
    session.add(
        OpportunitySignal(
            opportunity_id=opp.id,
            signal_id=signals[0].id,
            score=1.0,
            method="seed",
            tentative=False,
        )
    )
    await session.commit()
    return opp.id, signals[1].id, org.id, signals[0].id


async def test_new_customer_history_keeps_original_anchor_and_accepts_lifecycle_append(
    migrated_db, monkeypatch
):
    async with get_sessionmaker()() as setup:
        target_id, incoming_id, org_id, original_id = await world(setup)

    async def decide_after_customer_action(*args, **kwargs):
        async with get_sessionmaker()() as actor:
            await actor.get(Opportunity, target_id, with_for_update=True)
            actor.add(
                Brief(
                    org_id=org_id,
                    opportunity_id=target_id,
                    content_md="기존 신호로 작성한 분석",
                    model="test",
                    credits_spent=1,
                    idempotency_key=uuid4().hex,
                )
            )
            await actor.commit()
        return link.LinkDecision(target_id, 1.0, "ref", False, {})

    # The original bulk protection scan runs first; the customer commits before the chosen
    # row lock. This deterministically reproduces the race without timing-based sleeps.
    monkeypatch.setattr(link, "decide", decide_after_customer_action)
    async with get_sessionmaker()() as session:
        touched = await link.link_signals(session, RUNTIME, [incoming_id], today=TODAY)
        membership = await session.scalar(
            select(OpportunitySignal).where(OpportunitySignal.signal_id == incoming_id)
        )
        assert membership is not None and membership.method == "ref"
        assert membership.opportunity_id == target_id and touched == [target_id]
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OpportunitySignal)
                .where(OpportunitySignal.opportunity_id == target_id)
            )
            == 2
        )
        from app.db.models import OpportunityCustomerAnchor

        anchor = await session.get(OpportunityCustomerAnchor, target_id)
        assert anchor is not None and anchor.signal_ids == [original_id]
        await session.rollback()


async def test_new_human_review_between_selection_and_lock_still_prevents_attachment(
    migrated_db, monkeypatch
):
    async with get_sessionmaker()() as setup:
        target_id, incoming_id, _, original_id = await world(setup)

    async def decide_after_human_review(*args, **kwargs):
        async with get_sessionmaker()() as reviewer:
            await reviewer.get(Opportunity, target_id, with_for_update=True)
            reviewer.add(
                ReviewItem(
                    signal_id=original_id, status="approved", resolution={"action": "approve"}
                )
            )
            await reviewer.commit()
        return link.LinkDecision(target_id, 1.0, "ref", False, {})

    monkeypatch.setattr(link, "decide", decide_after_human_review)
    async with get_sessionmaker()() as session:
        touched = await link.link_signals(session, RUNTIME, [incoming_id], today=TODAY)
        membership = await session.scalar(
            select(OpportunitySignal).where(OpportunitySignal.signal_id == incoming_id)
        )
        assert membership is not None and membership.method == "seed"
        assert membership.opportunity_id != target_id and target_id not in touched
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OpportunitySignal)
                .where(OpportunitySignal.opportunity_id == target_id)
            )
            == 1
        )
        await session.rollback()


async def test_chosen_target_stays_locked_until_linking_transaction_commits(migrated_db):
    async with get_sessionmaker()() as setup:
        target_id, incoming_id, _, _ = await world(setup)
    async with get_sessionmaker()() as linker, get_sessionmaker()() as actor:
        assert await link.link_signals(linker, RUNTIME, [incoming_id], today=TODAY) == [target_id]
        with pytest.raises(DBAPIError) as blocked:
            async with actor.begin_nested():
                await actor.execute(
                    select(Opportunity.id)
                    .where(Opportunity.id == target_id)
                    .with_for_update(nowait=True)
                )
        assert blocked.value.orig.sqlstate == "55P03"
        await linker.commit()
        assert (
            await actor.scalar(
                select(Opportunity.id)
                .where(Opportunity.id == target_id)
                .with_for_update(nowait=True)
            )
            == target_id
        )
        await actor.rollback()
