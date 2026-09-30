"""Reviewed project→contract edges, independent of opportunity membership and forecasts.

Snapshots survive deleted/re-extracted signals. Every read rechecks their source identity;
an old stored confirmation is displayed as stale until a human reviews current evidence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.clock import now_utc
from app.db.models import (
    Document,
    InstitutionRow,
    Opportunity,
    OpportunityRelation,
    OpportunityRelationEvent,
    OpportunitySignal,
    Signal,
    User,
)
from app.domain.grounding import locate_quote

PROJECT_STAGES = ("council_mention", "budget_line")
CONTRACT_STAGES = ("order_plan", "prespec", "bid_notice", "award")
_DOCUMENT_STAGE = {"council_minutes": "council_mention", "budget_book": "budget_line"}
EvidenceRow = tuple[Signal, Document, OpportunitySignal | None]


class RelationValidationError(ValueError):
    def __init__(self, reasons: list[str]) -> None:
        self.reasons = sorted(set(reasons))
        super().__init__(", ".join(self.reasons))


class RelationConflictError(ValueError):
    """The caller must reload a changed version or use a new idempotency key."""


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _evidence_snapshot(
    signal: Signal, document: Document, membership: OpportunitySignal
) -> dict[str, Any]:
    source = document.text or ""
    if document.parse_status != "parsed" or not source.strip() or not document.content_hash:
        raise RelationValidationError([f"source_unavailable:{signal.id}"])
    if not signal.evidence:
        raise RelationValidationError([f"ungrounded_evidence:{signal.id}"])
    quotes = []
    for item in signal.evidence:
        quote, start, end = item.get("quote"), item.get("start"), item.get("end")
        if (
            not isinstance(quote, str)
            or not quote.strip()
            or item.get("found") is not True
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not 0 <= start < end <= len(source)
            or not locate_quote(source[start:end], quote, min_score=88).found
        ):
            raise RelationValidationError([f"ungrounded_evidence:{signal.id}"])
        quotes.append(
            {"quote": quote, "start": start, "end": end, "source_quote": source[start:end]}
        )
    snapshot: dict[str, Any] = {
        "signal_id": signal.id,
        "signal_title": signal.title,
        "stage": signal.stage,
        "opportunity_id": membership.opportunity_id,
        "document_id": document.id,
        "document_title": document.title,
        "document_url": document.url,
        "document_content_hash": document.content_hash,
        "source_text_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "evidence": quotes,
    }
    snapshot["fingerprint"] = _digest(
        {
            "snapshot": snapshot,
            "document_type": document.doc_type,
            "document_institution_code": document.institution_code,
            "published_at": document.published_at,
            "signal": {
                name: getattr(signal, name)
                for name in (
                    "dedupe_key",
                    "institution_code",
                    "stage",
                    "title",
                    "summary",
                    "category",
                    "budget_krw",
                    "expected_year",
                    "expected_half",
                    "commitment",
                    "confidence",
                    "observed_at",
                    "external_refs",
                    "evidence",
                    "grounding",
                )
            },
        }
    )
    return snapshot


def validate_evidence(
    project: Opportunity | None,
    contract: Opportunity | None,
    evidence_signal_ids: Sequence[int],
    rows: Sequence[EvidenceRow],
    *,
    institution_owners: Mapping[str, str | None] | None = None,
) -> list[dict[str, Any]]:
    reasons: list[str] = []
    if project is None or contract is None:
        raise RelationValidationError(["endpoint_missing"])
    if project.id == contract.id:
        reasons.append("self_relation")
    if not project.institution_code or project.institution_code != contract.institution_code:
        reasons.append("institution_mismatch")
    ids = set(evidence_signal_ids)
    if len(ids) < 2 or len(ids) > 20 or len(ids) != len(evidence_signal_ids):
        reasons.append("evidence_ids_invalid")
    found = {signal.id for signal, _, _ in rows}
    reasons.extend(f"missing_evidence:{sid}" for sid in sorted(ids - found))
    snapshots = []
    roles: set[str] = set()
    for signal, document, membership in sorted(rows, key=lambda row: row[0].id):
        if signal.id not in ids:
            continue
        if signal.verdict != "accepted" or membership is None or membership.tentative:
            reasons.append(f"evidence_not_eligible:{signal.id}")
            continue
        if signal.institution_code != project.institution_code:
            reasons.append(f"evidence_institution_mismatch:{signal.id}")
        source_code = document.institution_code
        source_owner = (
            (institution_owners or {}).get(source_code, source_code) if source_code else None
        )
        if source_code and source_owner != project.institution_code:
            reasons.append(f"source_institution_mismatch:{signal.id}")
        if signal.stage != _DOCUMENT_STAGE.get(document.doc_type, document.doc_type):
            reasons.append(f"evidence_stage_mismatch:{signal.id}")
        if membership.opportunity_id == project.id and signal.stage in PROJECT_STAGES:
            roles.add("project")
        elif membership.opportunity_id == contract.id and signal.stage in CONTRACT_STAGES:
            roles.add("contract")
        else:
            reasons.append(f"evidence_wrong_endpoint_or_stage:{signal.id}")
        try:
            snapshots.append(_evidence_snapshot(signal, document, membership))
        except RelationValidationError as exc:
            reasons.extend(exc.reasons)
    if roles != {"project", "contract"}:
        reasons.append("both_endpoint_roles_required")
    if reasons:
        raise RelationValidationError(reasons)
    return snapshots


def validity_reasons(
    relation: OpportunityRelation,
    project: Opportunity | None,
    contract: Opportunity | None,
    rows: Sequence[EvidenceRow],
    *,
    institution_owners: Mapping[str, str | None] | None = None,
) -> list[str]:
    try:
        current = validate_evidence(
            project,
            contract,
            relation.evidence_signal_ids,
            rows,
            institution_owners=institution_owners,
        )
    except RelationValidationError as exc:
        return exc.reasons
    saved = {item["signal_id"]: item.get("fingerprint") for item in relation.evidence_snapshot}
    actual = {item["signal_id"]: item["fingerprint"] for item in current}
    return [] if saved == actual else ["evidence_changed"]


async def _institution_owners(
    session: AsyncSession, rows: Sequence[EvidenceRow]
) -> dict[str, str | None]:
    """Use the same council→executive attribution as signal extraction, never a prefix guess."""
    codes = {document.institution_code for _, document, _ in rows if document.institution_code}
    if not codes:
        return {}
    institutions = await session.execute(
        select(InstitutionRow.code, InstitutionRow.kind, InstitutionRow.executive_code).where(
            InstitutionRow.code.in_(codes)
        )
    )
    return {
        code: executive_code if kind == "council" else code
        for code, kind, executive_code in institutions
    }


async def _load_evidence(session: AsyncSession, ids: Sequence[int]) -> list[EvidenceRow]:
    if not ids:
        return []
    signals = list(
        (
            await session.scalars(
                select(Signal)
                .options(defer(Signal.embedding))
                .where(Signal.id.in_(ids))
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    # Each original document is transferred once, even when several signals cite a big book.
    documents = {
        doc.id: doc
        for doc in await session.scalars(
            select(Document)
            .where(Document.id.in_({sig.document_id for sig in signals}))
            .execution_options(populate_existing=True)
        )
    }
    memberships = {
        member.signal_id: member
        for member in await session.scalars(
            select(OpportunitySignal)
            .where(OpportunitySignal.signal_id.in_(ids))
            .execution_options(populate_existing=True)
        )
    }
    return [
        (sig, documents[sig.document_id], memberships.get(sig.id))
        for sig in signals
        if sig.document_id in documents
    ]


async def _endpoints(session: AsyncSession, ids: Sequence[int]) -> dict[int, Opportunity]:
    return {
        opp.id: opp
        for opp in await session.scalars(
            select(Opportunity)
            .options(defer(Opportunity.embedding))
            .where(Opportunity.id.in_(ids))
            .execution_options(populate_existing=True)
        )
    }


async def _would_cycle(session: AsyncSession, project_id: int, contract_id: int) -> bool:
    edges = OpportunityRelation
    reachable = (
        select(edges.contract_id)
        .where(edges.project_id == contract_id, edges.status.in_(("proposed", "confirmed")))
        .cte("reachable_contract", recursive=True)
    )
    reachable = reachable.union(
        select(edges.contract_id)
        .join(reachable, edges.project_id == reachable.c.contract_id)
        .where(edges.status.in_(("proposed", "confirmed")))
    )
    return bool(
        await session.scalar(
            select(reachable.c.contract_id).where(reachable.c.contract_id == project_id).limit(1)
        )
    )


async def has_relation_review_for_document(session: AsyncSession, document_id: int) -> bool:
    """Protect original documents using immutable history even if a signal has disappeared."""
    return (
        await session.scalar(
            select(OpportunityRelationEvent.id)
            .where(
                OpportunityRelationEvent.evidence_snapshot.contains([{"document_id": document_id}])
            )
            .limit(1)
        )
    ) is not None


async def decide_relation(
    session: AsyncSession,
    *,
    project_id: int,
    contract_id: int,
    status: str,
    expected_version: int,
    evidence_signal_ids: list[int],
    note: str,
    actor: User,
    idempotency_key: str,
) -> OpportunityRelation:
    if not actor.is_staff:
        raise PermissionError("staff_required")
    if status not in ("proposed", "confirmed", "rejected") or not note.strip() or len(note) > 2000:
        raise RelationValidationError(["invalid_decision"])
    if not 8 <= len(idempotency_key) <= 128 or expected_version < 0:
        raise RelationValidationError(["invalid_request_identity"])
    request_digest = _digest(
        {
            "actor": actor.id,
            "project_id": project_id,
            "contract_id": contract_id,
            "status": status,
            "expected_version": expected_version,
            "evidence_signal_ids": sorted(evidence_signal_ids),
            "note": note,
        }
    )
    # Low-volume human decisions serialize graph/uniqueness checks, including first insert.
    # Ingestion never needs this lock; no external effects occur inside the transaction.
    await session.execute(text("SELECT pg_advisory_xact_lock(704230913)"))
    previous = await session.scalar(
        select(OpportunityRelationEvent).where(
            OpportunityRelationEvent.idempotency_key == idempotency_key
        )
    )
    if previous is not None:
        if previous.request_digest != request_digest:
            raise RelationConflictError("idempotency_key_reused")
        relation = await session.get(
            OpportunityRelation, previous.relation_id, populate_existing=True
        )
        assert relation is not None
        return relation  # Retries never undo a newer decision; reads return the current version.
    # Keep endpoint membership stable from validation through the durable decision. Use a
    # common order before Signal locks so automatic regrouping cannot invalidate our reads.
    await session.execute(
        select(Opportunity.id)
        .where(Opportunity.id.in_(sorted({project_id, contract_id})))
        .order_by(Opportunity.id)
        .with_for_update()
    )
    relation = await session.scalar(
        select(OpportunityRelation)
        .where(
            OpportunityRelation.project_id == project_id,
            OpportunityRelation.contract_id == contract_id,
            OpportunityRelation.kind == "project_contract",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if expected_version != (relation.version if relation else 0):
        raise RelationConflictError("stale_version")
    if project_id == contract_id:
        raise RelationValidationError(["self_relation"])
    ids = sorted(evidence_signal_ids)
    if status == "rejected":
        if relation is None:
            raise RelationValidationError(["relation_missing"])
        # Revocation must remain possible when the old sources have vanished.
        ids, snapshots = relation.evidence_signal_ids, relation.evidence_snapshot
    else:
        # Reprocessing locks these same rows before checking review history. Holding them
        # until our event is committed prevents it from checking too early and deleting
        # newly reviewed evidence. Never lock Document here: ingestion locks it first.
        await session.execute(
            select(Signal.id).where(Signal.id.in_(ids)).order_by(Signal.id).with_for_update()
        )
        endpoints = await _endpoints(session, [project_id, contract_id])
        rows = await _load_evidence(session, ids)
        snapshots = validate_evidence(
            endpoints.get(project_id),
            endpoints.get(contract_id),
            ids,
            rows,
            institution_owners=await _institution_owners(session, rows),
        )
        if await _would_cycle(session, project_id, contract_id):
            raise RelationValidationError(["relation_cycle"])
        if status == "confirmed":
            other = await session.scalar(
                select(OpportunityRelation.id)
                .where(
                    OpportunityRelation.contract_id == contract_id,
                    OpportunityRelation.project_id != project_id,
                    OpportunityRelation.status == "confirmed",
                )
                .limit(1)
            )
            if other is not None:
                raise RelationConflictError("contract_already_has_confirmed_project")
    if relation is None:
        relation = OpportunityRelation(project_id=project_id, contract_id=contract_id, version=1)
        session.add(relation)
    else:
        relation.version += 1
    relation.status = status
    relation.evidence_signal_ids = list(ids)
    relation.evidence_snapshot = snapshots
    relation.note = note
    relation.updated_by = actor.id
    relation.updated_at = now_utc()
    await session.flush()
    session.add(
        OpportunityRelationEvent(
            relation_id=relation.id,
            version=relation.version,
            status=status,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            actor_user_id=actor.id,
            actor_snapshot={"id": actor.id, "name": actor.name},
            evidence_signal_ids=list(ids),
            evidence_snapshot=snapshots,
            note=note,
        )
    )
    await session.flush()
    return relation


async def relation_views(
    session: AsyncSession,
    relations: Sequence[OpportunityRelation],
    *,
    include_history: bool = False,
    include_private: bool = False,
) -> list[dict[str, Any]]:
    if not relations:
        return []
    endpoints = await _endpoints(
        session, list({i for rel in relations for i in (rel.project_id, rel.contract_id)})
    )
    evidence = await _load_evidence(
        session, sorted({i for rel in relations for i in rel.evidence_signal_ids})
    )
    by_signal = {row[0].id: row for row in evidence}
    institution_owners = await _institution_owners(session, evidence)
    histories: dict[int, list[dict[str, Any]]] = {}
    if include_history:
        events = await session.scalars(
            select(OpportunityRelationEvent)
            .where(OpportunityRelationEvent.relation_id.in_([rel.id for rel in relations]))
            .order_by(OpportunityRelationEvent.relation_id, OpportunityRelationEvent.version)
        )
        for event in events:
            histories.setdefault(event.relation_id, []).append(
                {
                    "version": event.version,
                    "status": event.status,
                    "note": event.note,
                    "actor_user_id": event.actor_snapshot.get("id"),
                    "actor_name": event.actor_snapshot.get("name"),
                    "created_at": event.created_at,
                    "evidence_signal_ids": event.evidence_signal_ids,
                    "evidence": event.evidence_snapshot,
                }
            )
    result = []
    for relation in relations:
        project, contract = endpoints.get(relation.project_id), endpoints.get(relation.contract_id)
        rows = [by_signal[sid] for sid in relation.evidence_signal_ids if sid in by_signal]
        reasons = validity_reasons(
            relation, project, contract, rows, institution_owners=institution_owners
        )
        result.append(
            {
                "id": relation.id,
                "kind": relation.kind,
                "project_id": relation.project_id,
                "contract_id": relation.contract_id,
                "status": relation.status,
                "effective_status": "stale"
                if reasons and relation.status != "rejected"
                else relation.status,
                "valid": not reasons,
                "validity_reasons": reasons,
                "version": relation.version,
                "evidence_signal_ids": relation.evidence_signal_ids,
                "evidence": relation.evidence_snapshot,
                "note": relation.note if include_private or include_history else "",
                "updated_by": relation.updated_by if include_private or include_history else None,
                "created_at": relation.created_at,
                "updated_at": relation.updated_at,
                "project": _endpoint_view(project),
                "contract": _endpoint_view(contract),
                "history": histories.get(relation.id, []),
            }
        )
    return result


def _endpoint_view(opp: Opportunity | None) -> dict[str, Any] | None:
    return (
        {name: getattr(opp, name) for name in ("id", "title", "institution_code", "stage")}
        if opp
        else None
    )


async def list_relations(
    session: AsyncSession,
    *,
    opportunity_id: int | None = None,
    status: str | None = None,
    after_id: int = 0,
    limit: int = 50,
    include_history: bool = False,
    include_private: bool = False,
) -> dict[str, Any]:
    if not 1 <= limit <= 100 or after_id < 0:
        raise ValueError("invalid_page")
    stmt = select(OpportunityRelation).where(OpportunityRelation.id > after_id)
    if opportunity_id is not None:
        stmt = stmt.where(
            or_(
                OpportunityRelation.project_id == opportunity_id,
                OpportunityRelation.contract_id == opportunity_id,
            )
        )
    if status is not None:
        stmt = stmt.where(OpportunityRelation.status == status)
    relations = list(
        (await session.scalars(stmt.order_by(OpportunityRelation.id).limit(limit + 1))).all()
    )
    more = len(relations) > limit
    relations = relations[:limit]
    return {
        "items": await relation_views(
            session, relations, include_history=include_history, include_private=include_private
        ),
        "next_after_id": relations[-1].id if more else None,
    }


async def mixed_group_audit(
    session: AsyncSession, *, after_id: int = 0, limit: int = 50
) -> dict[str, Any]:
    """List legacy mixed memberships for review; do not split or endorse them."""
    if not 1 <= limit <= 100 or after_id < 0:
        raise ValueError("invalid_page")
    ids = list(
        (
            await session.scalars(
                select(OpportunitySignal.opportunity_id)
                .join(Signal, Signal.id == OpportunitySignal.signal_id)
                .where(
                    OpportunitySignal.opportunity_id > after_id,
                    Signal.verdict == "accepted",
                    OpportunitySignal.tentative.is_(False),
                )
                .group_by(OpportunitySignal.opportunity_id)
                .having(
                    func.count().filter(Signal.stage.in_(PROJECT_STAGES)) > 0,
                    func.count().filter(Signal.stage.in_(CONTRACT_STAGES)) > 0,
                )
                .order_by(OpportunitySignal.opportunity_id)
                .limit(limit + 1)
            )
        ).all()
    )
    more, ids = len(ids) > limit, ids[:limit]
    endpoints = await _endpoints(session, ids)
    members = (
        await session.execute(
            select(OpportunitySignal.opportunity_id, Signal.id, Signal.stage, Signal.external_refs)
            .join(Signal, Signal.id == OpportunitySignal.signal_id)
            .where(
                OpportunitySignal.opportunity_id.in_(ids),
                Signal.verdict == "accepted",
                OpportunitySignal.tentative.is_(False),
            )
            .order_by(Signal.id)
        )
    ).all()
    items = []
    for opp_id in ids:
        rows = [row for row in members if row[0] == opp_id]
        items.append(
            {
                "opportunity": _endpoint_view(endpoints[opp_id]),
                "project_signal_ids": [row[1] for row in rows if row[2] in PROJECT_STAGES],
                "contract_signal_ids": [row[1] for row in rows if row[2] in CONTRACT_STAGES],
                "bid_notice_numbers": sorted(
                    {str(row[3]["bid_notice_no"]) for row in rows if row[3].get("bid_notice_no")}
                ),
                "reason": "mixed_primary_membership_requires_review",
            }
        )
    return {"items": items, "next_after_id": ids[-1] if more else None}
