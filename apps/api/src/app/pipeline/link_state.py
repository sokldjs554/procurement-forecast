"""Shared completion visibility and immutable customer/reviewer opportunity anchors."""

from typing import Any

from sqlalchemy import BigInteger, Select, any_, cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, array, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import (
    Brief,
    LinkReconciliationState,
    Notification,
    Opportunity,
    OpportunityCustomerAnchor,
    OpportunityRelation,
    OpportunityRelationEvent,
    OpportunitySignal,
    Recommendation,
    ReviewItem,
    Signal,
)


def link_settled() -> ColumnElement[bool]:
    return (
        ~select(LinkReconciliationState.institution_code)
        .where(
            LinkReconciliationState.institution_code == Opportunity.institution_code,
            LinkReconciliationState.generation > LinkReconciliationState.reconciled_generation,
        )
        .exists()
    )


def protected_opportunities_query(
    codes: set[str],
    *,
    opportunity_ids: set[int] | None = None,
    include_customer: bool = True,
) -> Select[Any]:
    """Strict reviewed identities, optionally including customer-history identities.

    Start with the institutions' identities, including foreign-owner groups containing
    their signals. JSON history is compared to trusted numeric IDs using containment;
    malformed legacy fields never undergo bigint casts or array expansion. No evidence
    text, embedding, full review resolution or notification payload leaves PostgreSQL.
    Reconciliation callers use ``include_customer=False`` and separately preserve each
    customer's immutable signal core; customer history alone must not freeze new arrivals.
    """
    direct = select(Opportunity.id).where(Opportunity.institution_code.in_(sorted(codes)))
    members = (
        select(OpportunitySignal.opportunity_id)
        .join(Signal, Signal.id == OpportunitySignal.signal_id)
        .where(Signal.institution_code.in_(sorted(codes)))
    )
    if opportunity_ids is not None:
        direct = direct.where(Opportunity.id.in_(sorted(opportunity_ids)))
        members = members.where(OpportunitySignal.opportunity_id.in_(sorted(opportunity_ids)))
    scope = (
        direct.union(members)
        .cte("link_protection_scope")
        .prefix_with("MATERIALIZED", dialect="postgresql")
    )
    oid = Opportunity.id
    anchor = func.jsonb_build_object("opportunity_id", oid)
    previous = func.jsonb_build_object("previous_link", anchor)
    unsafe_member = (
        select(OpportunitySignal.signal_id)
        .join(Signal, Signal.id == OpportunitySignal.signal_id)
        .where(
            OpportunitySignal.opportunity_id == oid,
            or_(
                OpportunitySignal.method == "manual",
                OpportunitySignal.tentative.is_(True),
                Signal.verdict != "accepted",
                Signal.institution_code.is_distinct_from(Opportunity.institution_code),
            ),
        )
        .correlate(Opportunity)
        .exists()
    )
    reviewed_member = (
        select(ReviewItem.id)
        .join(OpportunitySignal, OpportunitySignal.signal_id == ReviewItem.signal_id)
        .where(
            OpportunitySignal.opportunity_id == oid,
            or_(
                ReviewItem.status != "open",
                ReviewItem.resolved_at.is_not(None),
                ReviewItem.resolved_by.is_not(None),
                # Preserve nonempty legacy decisions even when their status was reset.
                (ReviewItem.resolution != cast(literal("{}"), JSONB))
                & ReviewItem.resolution.is_distinct_from(cast(literal("null"), JSONB)),
            ),
        )
        .correlate(Opportunity)
        .exists()
    )
    # EXISTS may choose a sequential first-match scan for parameterized JSONB history,
    # then scan the whole archive for each absent identity. Counting indexed matches
    # costs all matching entries but avoids that optimistic first-row plan on misses.
    reviewed_history = (
        select(func.count(ReviewItem.id))
        .where(
            or_(
                ReviewItem.resolution.contains(previous),
                ReviewItem.resolution.contains(
                    func.jsonb_build_object("history", func.jsonb_build_array(previous))
                ),
            )
        )
        .correlate(Opportunity)
        .scalar_subquery()
        > 0
    )
    relation_endpoint = (
        select(func.count(OpportunityRelation.id))
        .where(or_(OpportunityRelation.project_id == oid, OpportunityRelation.contract_id == oid))
        .correlate(Opportunity)
        .scalar_subquery()
        > 0
    )
    relation_history = (
        select(func.count(OpportunityRelationEvent.id))
        .where(OpportunityRelationEvent.evidence_snapshot.contains(func.jsonb_build_array(anchor)))
        .correlate(Opportunity)
        .scalar_subquery()
        > 0
    )
    moved_relation_evidence = (
        select(func.count(OpportunitySignal.signal_id))
        .join(
            OpportunityRelationEvent,
            OpportunityRelationEvent.evidence_signal_ids.contains(
                array([OpportunitySignal.signal_id], type_=BigInteger)
            ),
        )
        .where(OpportunitySignal.opportunity_id == oid)
        .correlate(Opportunity)
        .scalar_subquery()
        > 0
    )
    eligible_core_count = (
        select(func.count(Signal.id))
        .join(OpportunitySignal, OpportunitySignal.signal_id == Signal.id)
        .where(
            OpportunitySignal.opportunity_id == oid,
            OpportunitySignal.tentative.is_(False),
            Signal.verdict == "accepted",
            Signal.institution_code == Opportunity.institution_code,
            Signal.id == any_(OpportunityCustomerAnchor.signal_ids),
        )
        .correlate(Opportunity, OpportunityCustomerAnchor)
        .scalar_subquery()
    )
    invalid_customer_core = (
        select(OpportunityCustomerAnchor.opportunity_id)
        .where(
            OpportunityCustomerAnchor.opportunity_id == oid,
            or_(
                func.coalesce(func.cardinality(OpportunityCustomerAnchor.signal_ids), 0) == 0,
                eligible_core_count != func.cardinality(OpportunityCustomerAnchor.signal_ids),
            ),
        )
        .correlate(Opportunity)
        .exists()
    )
    conditions: list[ColumnElement[bool]] = [
        unsafe_member,
        reviewed_member,
        reviewed_history,
        relation_endpoint,
        relation_history,
        moved_relation_evidence,
        invalid_customer_core,
    ]
    if include_customer:
        conditions.append(customer_history_predicate())
    return select(oid).join(scope, scope.c.id == oid).where(or_(*conditions)).order_by(oid)


def customer_history_predicate() -> ColumnElement[bool]:
    """Durable customer history for the surrounding Opportunity row, without loading it."""
    oid = Opportunity.id
    anchor = func.jsonb_build_object("opportunity_id", oid)
    customer_action = (
        select(func.count(Recommendation.opportunity_id))
        .where(
            Recommendation.opportunity_id == oid,
            or_(
                Recommendation.feedback.is_not(None),
                Recommendation.feedback_at.is_not(None),
                Recommendation.notified_stage.is_not(None),
            ),
        )
        .correlate(Opportunity)
        .scalar_subquery()
        > 0
    )
    brief = (
        select(func.count(Brief.opportunity_id))
        .where(Brief.opportunity_id == oid)
        .correlate(Opportunity)
        .scalar_subquery()
        > 0
    )
    notification = (
        select(func.count(Notification.id))
        .where(
            Notification.payload.contains(
                func.jsonb_build_object("items", func.jsonb_build_array(anchor))
            )
        )
        .correlate(Opportunity)
        .scalar_subquery()
        > 0
    )
    return or_(customer_action, brief, notification)


async def protected_opportunity_ids(
    session: AsyncSession,
    institution_codes: set[str],
    *,
    opportunity_ids: set[int] | None = None,
    include_customer: bool = True,
) -> set[int]:
    if not institution_codes:
        return set()
    return set(
        (
            await session.scalars(
                protected_opportunities_query(
                    institution_codes,
                    opportunity_ids=opportunity_ids,
                    include_customer=include_customer,
                )
            )
        ).all()
    )


def customer_reprocessing_query(signal_ids: list[int]) -> Any:
    """Protect original published evidence, including history predating core snapshots.

    For a stored core, only its members are immutable. Without a stored core, any
    current member of a published identity could be an original member; never delete
    those IDs before the first lazy capture. Call after locking affected identities.
    """
    stored = select(OpportunityCustomerAnchor.opportunity_id).where(
        OpportunityCustomerAnchor.signal_ids.overlap(cast(signal_ids, ARRAY(BigInteger)))
    )
    legacy = select(Opportunity.id).where(
        Opportunity.id.in_(
            select(OpportunitySignal.opportunity_id).where(
                OpportunitySignal.signal_id.in_(signal_ids)
            )
        ),
        ~select(OpportunityCustomerAnchor.opportunity_id)
        .where(OpportunityCustomerAnchor.opportunity_id == Opportunity.id)
        .exists(),
        customer_history_predicate(),
    )
    return stored.union_all(legacy).limit(1)


async def capture_customer_anchor(
    session: AsyncSession, opportunity_id: int
) -> OpportunityCustomerAnchor:
    """Capture the first customer evidence core; caller must hold Opportunity FOR UPDATE.

    Actor writers call before first publication/feedback. Existing cores never grow with
    automatic arrivals. Empty cores are retained as empty so reconciliation can treat that
    invalid identity conservatively instead of silently adopting later evidence.
    """
    existing = await session.get(OpportunityCustomerAnchor, opportunity_id)
    if existing is not None:
        return existing
    signal_ids = list(
        (
            await session.scalars(
                select(Signal.id)
                .join(OpportunitySignal, OpportunitySignal.signal_id == Signal.id)
                .where(
                    OpportunitySignal.opportunity_id == opportunity_id,
                    OpportunitySignal.tentative.is_(False),
                    Signal.verdict == "accepted",
                )
                .order_by(Signal.id)
            )
        ).all()
    )
    await session.execute(
        insert(OpportunityCustomerAnchor)
        .values(opportunity_id=opportunity_id, signal_ids=signal_ids)
        .on_conflict_do_nothing(index_elements=[OpportunityCustomerAnchor.opportunity_id])
    )
    stored = await session.get(OpportunityCustomerAnchor, opportunity_id, populate_existing=True)
    if stored is None:
        raise RuntimeError("Customer anchor was not persisted")
    return stored


async def ensure_customer_anchor(
    session: AsyncSession, opportunity_id: int
) -> OpportunityCustomerAnchor | None:
    """Capture legacy customer history before append; caller holds Opportunity FOR UPDATE."""
    existing = await session.get(OpportunityCustomerAnchor, opportunity_id)
    if existing is not None:
        return existing
    customer_id = await session.scalar(
        select(Opportunity.id).where(Opportunity.id == opportunity_id, customer_history_predicate())
    )
    if customer_id is None:
        return None
    return await capture_customer_anchor(session, opportunity_id)
