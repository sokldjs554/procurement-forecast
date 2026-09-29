"""Converge automatic links after ingestion, preserving decisions and identity history.

Only already-linked evidence participates. The worker persists dirty generations before
enqueueing this stage, so a crash cannot silently leave a completed run order-dependent.
The caller owns the transaction. No external calls or notifications are made here.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import today_kst
from app.db.models import (
    Document,
    DocumentChunk,
    LinkReconciliationEvent,
    LinkReconciliationState,
    Opportunity,
    OpportunitySignal,
    Signal,
    Source,
)
from app.pipeline.revalidate import _digest, _snapshot, refresh_affected_recommendations
from app.runtime import Runtime

VERSION = "canonical-links-v1"
SUMMARY_FIELDS = (
    "institution_code",
    "department",
    "title",
    "category",
    "stage",
    "status",
    "first_seen_at",
    "last_signal_at",
    "bid_window_start",
    "bid_window_end",
    "bid_published_at",
    "est_budget_krw",
    "best_commitment",
    "signal_count",
    "conversion_prob",
    "keywords",
    "embedding",
)


class LinkSnapshotChangedError(RuntimeError):
    """Retry from a fresh snapshot; no membership changes have been applied."""


@dataclass(slots=True)
class ReconciliationResult:
    touched_ids: list[int]
    protected_opportunities: int
    changed_signals: int
    generation: int
    processed: bool
    ambiguous_keys: int = 0


@dataclass(slots=True)
class _Scope:
    signals: list[Signal]
    links: dict[int, OpportunitySignal]
    opportunities: dict[int, Opportunity]
    keys: dict[int, str]
    protected: set[int]
    digest: str


def assign_existing_ids(
    groups: list[set[int]], old_members: dict[int, set[int]]
) -> list[int | None]:
    """Keep exact identities first, then greatest overlap; never reuse unrelated IDs.

    Memberships, not generated IDs, determine canonical groups. This separate matching
    merely keeps existing URLs/customer history anchored to surviving evidence.
    """
    result: list[int | None] = [None] * len(groups)
    available = set(old_members)
    exact = {frozenset(members): oid for oid, members in old_members.items() if members}
    for i, members in enumerate(groups):
        oid = exact.get(frozenset(members))
        if oid is not None:
            result[i] = oid
            available.remove(oid)
    # A global overlap order avoids an early small fragment stealing the identity from
    # its larger surviving group. Stable group order breaks equal-sized splits.
    overlaps = sorted(
        (-len(members & old_members[oid]), i, oid)
        for i, members in enumerate(groups)
        if result[i] is None
        for oid in available
        if members & old_members[oid]
    )
    for _, i, oid in overlaps:
        if result[i] is None and oid in available:
            result[i] = oid
            available.remove(oid)
    return result


async def mark_link_dirty(session: AsyncSession, institution_codes: set[str]) -> None:
    if not institution_codes:
        return
    stmt = insert(LinkReconciliationState).values(
        [{"institution_code": code, "generation": 1} for code in sorted(institution_codes)]
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[LinkReconciliationState.institution_code],
            set_={"generation": LinkReconciliationState.generation + 1},
        )
    )


def reconciliation_scope_ids(institution_code: str) -> Any:
    """Include foreign-owner legacy memberships so they can be protected, never hidden."""
    return (
        select(Opportunity.id)
        .where(Opportunity.institution_code == institution_code)
        .union(
            select(OpportunitySignal.opportunity_id)
            .join(Signal, Signal.id == OpportunitySignal.signal_id)
            .where(Signal.institution_code == institution_code)
        )
    )


def reconciliation_signals_query(institution_code: str) -> Any:
    return (
        select(
            Signal,
            OpportunitySignal,
            Source.key,
            Document.external_id,
            Document.doc_type,
            DocumentChunk.seq,
            DocumentChunk.char_start,
            DocumentChunk.char_end,
            DocumentChunk.labels,
        )
        .join(OpportunitySignal, OpportunitySignal.signal_id == Signal.id)
        .join(Document, Document.id == Signal.document_id)
        .join(Source, Source.id == Document.source_id)
        .outerjoin(DocumentChunk, DocumentChunk.id == Signal.chunk_id)
        .where(OpportunitySignal.opportunity_id.in_(reconciliation_scope_ids(institution_code)))
        .order_by(Signal.id)
        .execution_options(populate_existing=True)
    )


async def _load_scope(session: AsyncSession, institution_code: str, generation: int) -> _Scope:
    from app.pipeline.link_partition import stable_signal_key

    rows = (await session.execute(reconciliation_signals_query(institution_code))).all()
    signals: list[Signal] = []
    links: dict[int, OpportunitySignal] = {}
    keys: dict[int, str] = {}
    for signal, link, source, external, kind, seq, start, end, labels in rows:
        signals.append(signal)
        links[signal.id] = link
        keys[signal.id] = stable_signal_key(
            signal,
            source_key=source,
            document_external_id=external,
            document_type=kind,
            chunk_seq=seq,
            chunk_start=start,
            chunk_end=end,
            chunk_labels=labels or (),
        )
    opportunities = {
        o.id: o
        for o in await session.scalars(
            select(Opportunity)
            .where(Opportunity.id.in_(reconciliation_scope_ids(institution_code)))
            .execution_options(populate_existing=True)
        )
    }
    from app.pipeline.link_state import protected_opportunity_ids

    protected = await protected_opportunity_ids(session, {institution_code})
    fingerprint = {
        "version": VERSION,
        "institution": institution_code,
        "generation": generation,
        "signals": [_snapshot(s) for s in signals],
        "links": [_snapshot(links[s.id]) for s in signals],
        "keys": sorted(keys.items()),
        "protected": sorted(protected),
        "opportunities": [_snapshot(opportunities[oid]) for oid in sorted(opportunities)],
    }
    return _Scope(signals, links, opportunities, keys, protected, _digest(fingerprint))


def _membership(link: OpportunitySignal) -> dict[str, Any]:
    return {
        "opportunity_id": link.opportunity_id,
        "method": link.method,
        "score": link.score,
        "tentative": link.tentative,
        "reasons": link.reasons,
    }


async def reconcile_institution(
    session: AsyncSession,
    runtime: Runtime,
    institution_code: str,
    *,
    today: date | None = None,
    calibration: dict[str, float] | None = None,
) -> ReconciliationResult:
    from app.pipeline.link import summarize_opportunity
    from app.pipeline.link_partition import plan_partition

    business_date = today or today_kst()
    await session.flush()
    state = await session.get(LinkReconciliationState, institution_code, populate_existing=True)
    if state is None or state.generation == state.reconciled_generation:
        return ReconciliationResult([], 0, 0, state.generation if state else 0, False)
    generation = state.generation
    scope = await _load_scope(session, institution_code, generation)
    eligible = [s for s in scope.signals if scope.links[s.id].opportunity_id not in scope.protected]
    # Pure CPU planning must not block worker health checks/queue heartbeats. All Signal
    # attributes are eagerly loaded; the planner never reads SQL or mutates these inputs.
    groups = await asyncio.to_thread(
        plan_partition,
        eligible,
        scope.keys,
        threshold=runtime.settings.link_threshold,
        review_band=runtime.settings.link_review_band,
        today=business_date,
        calibration=calibration,
    )
    old_members: dict[int, set[int]] = defaultdict(set)
    for s in eligible:
        old_members[scope.links[s.id].opportunity_id].add(s.id)
    assigned = assign_existing_ids([{s.id for s in g.members} for g in groups], old_members)
    async with session.begin_nested():
        # Expensive planning occurs before these short locks. Re-read every input under the
        # lock: generation alone cannot detect a simultaneous human decision or source edit.
        await session.execute(text("SET LOCAL lock_timeout = '5s'"))
        await session.execute(
            text(
                "LOCK TABLE sources, documents, document_chunks, signals, review_items, "
                "opportunity_signals, opportunities, recommendations, company_profiles, "
                "institutions, opportunity_relations, opportunity_relation_events, briefs, "
                "notifications, link_reconciliation_states IN SHARE ROW EXCLUSIVE MODE"
            )
        )
        state = await session.get(LinkReconciliationState, institution_code, populate_existing=True)
        if (
            state is None
            or state.generation != generation
            or state.reconciled_generation == generation
        ):
            raise LinkSnapshotChangedError("Link generation changed during reconciliation")
        current = await _load_scope(session, institution_code, generation)
        if current.digest != scope.digest:
            raise LinkSnapshotChangedError(
                "Link evidence or protected decisions changed during planning"
            )
        touched: set[int] = set(old_members)
        changed = 0
        for group, assigned_id in zip(groups, assigned, strict=True):
            if assigned_id is None:
                opportunity = Opportunity()
                for field in SUMMARY_FIELDS:
                    setattr(opportunity, field, getattr(group.summary, field))
                session.add(opportunity)
                await session.flush()
                oid = opportunity.id
            else:
                opportunity = current.opportunities[assigned_id]
                for field in SUMMARY_FIELDS:
                    setattr(opportunity, field, getattr(group.summary, field))
                oid = assigned_id
            touched.add(oid)
            for signal in group.members:
                link = current.links[signal.id]
                decision = group.decisions[signal.id]
                before = _membership(link)
                after = {
                    "opportunity_id": oid,
                    "score": decision.score,
                    "method": decision.method,
                    "tentative": decision.tentative,
                    "reasons": decision.reasons | {"reconciliation_version": VERSION},
                }
                if before != after:
                    session.add(
                        LinkReconciliationEvent(
                            institution_code=institution_code,
                            generation=generation,
                            signal_id=signal.id,
                            stable_key=scope.keys[signal.id],
                            before=before,
                            after=after,
                            input_digest=scope.digest,
                        )
                    )
                    changed += before["opportunity_id"] != oid
                    for name, value in after.items():
                        setattr(link, name, value)
        # Updating a link's composite PK retains its created_at and avoids a delete/insert
        # window. Old opportunity rows are never removed or repurposed without overlap.
        for oid in set(old_members) - {i for i in assigned if i is not None}:
            summarize_opportunity(
                current.opportunities[oid], [], today=business_date, calibration=calibration
            )
        await session.flush()
        await refresh_affected_recommendations(
            session, sorted(touched), today=business_date, audit_digest=scope.digest
        )
        state.reconciled_generation = generation
        await session.flush()
    return ReconciliationResult(
        sorted(touched),
        len(scope.protected),
        changed,
        generation,
        True,
        len({key for group in groups for key in group.ambiguous_keys}),
    )


async def reconcile_pending(
    session: AsyncSession,
    runtime: Runtime,
    *,
    today: date | None = None,
) -> list[ReconciliationResult]:
    """Synchronous pipeline completion; caller commits each unit or the whole run."""
    codes = list(
        await session.scalars(
            select(LinkReconciliationState.institution_code)
            .where(
                LinkReconciliationState.generation > LinkReconciliationState.reconciled_generation
            )
            .order_by(LinkReconciliationState.institution_code)
        )
    )
    return [await reconcile_institution(session, runtime, code, today=today) for code in codes]
