"""Shared completion visibility and immutable customer/reviewer opportunity anchors."""

from typing import Any

from sqlalchemy import BigInteger, Select, cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import JSONB, array
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import (
    Brief,
    LinkReconciliationState,
    Notification,
    Opportunity,
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


def protected_opportunities_query(codes: set[str]) -> Select[Any]:
    """IDs that automatic linking must neither mutate nor accept new members into.

    Start with the institutions' identities, including foreign-owner groups containing
    their signals. JSON history is compared to trusted numeric IDs using containment;
    malformed legacy fields never undergo bigint casts or array expansion. No evidence
    text, embedding, full review resolution or notification payload leaves PostgreSQL.
    """
    scope = (
        select(Opportunity.id)
        .where(Opportunity.institution_code.in_(sorted(codes)))
        .union(
            select(OpportunitySignal.opportunity_id)
            .join(Signal, Signal.id == OpportunitySignal.signal_id)
            .where(Signal.institution_code.in_(sorted(codes)))
        )
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
    reviewed_history = (
        select(ReviewItem.id)
        .where(
            or_(
                ReviewItem.resolution.contains(previous),
                ReviewItem.resolution.contains(
                    func.jsonb_build_object("history", func.jsonb_build_array(previous))
                ),
            )
        )
        .correlate(Opportunity)
        .exists()
    )
    relation_endpoint = (
        select(OpportunityRelation.id)
        .where(or_(OpportunityRelation.project_id == oid, OpportunityRelation.contract_id == oid))
        .correlate(Opportunity)
        .exists()
    )
    relation_history = (
        select(OpportunityRelationEvent.id)
        .where(OpportunityRelationEvent.evidence_snapshot.contains(func.jsonb_build_array(anchor)))
        .correlate(Opportunity)
        .exists()
    )
    moved_relation_evidence = (
        select(OpportunitySignal.signal_id)
        .join(
            OpportunityRelationEvent,
            OpportunityRelationEvent.evidence_signal_ids.contains(
                array([OpportunitySignal.signal_id], type_=BigInteger)
            ),
        )
        .where(OpportunitySignal.opportunity_id == oid)
        .correlate(Opportunity)
        .exists()
    )
    customer_action = (
        select(Recommendation.opportunity_id)
        .where(
            Recommendation.opportunity_id == oid,
            or_(
                Recommendation.feedback.is_not(None),
                Recommendation.feedback_at.is_not(None),
                Recommendation.notified_stage.is_not(None),
            ),
        )
        .correlate(Opportunity)
        .exists()
    )
    brief = (
        select(Brief.opportunity_id)
        .where(Brief.opportunity_id == oid)
        .correlate(Opportunity)
        .exists()
    )
    notification = (
        select(Notification.id)
        .where(
            Notification.payload.contains(
                func.jsonb_build_object("items", func.jsonb_build_array(anchor))
            )
        )
        .correlate(Opportunity)
        .exists()
    )
    return (
        select(oid)
        .join(scope, scope.c.id == oid)
        .where(
            or_(
                unsafe_member,
                reviewed_member,
                reviewed_history,
                relation_endpoint,
                relation_history,
                moved_relation_evidence,
                customer_action,
                brief,
                notification,
            )
        )
        .order_by(oid)
    )


async def protected_opportunity_ids(session: AsyncSession, institution_codes: set[str]) -> set[int]:
    if not institution_codes:
        return set()
    return set((await session.scalars(protected_opportunities_query(institution_codes))).all())
