"""Canonical automatic partition planning, with no SQL or mutations of persisted inputs.

The caller supplies the complete eligible, already-linked universe for an institution and
excludes strict human/unsafe protections. Customer history anchors its original members;
later automatic attachments are replayed like every other automatic member. Old automatic
memberships are deliberately not read. Applying the plan belongs to the transaction layer.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.db.models import Opportunity, Signal
from app.pipeline.link import (
    _REF_KEYS,
    SPAN_DAYS,
    LinkDecision,
    _bid_numbers,
    _ReferenceConflictError,
    choose_similarity,
    reference_target,
    summarize_opportunity,
    without_conflicting_numbers,
    without_other_budget_rows,
)
from app.pipeline.process import canonical_title


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def stable_signal_key(
    signal: Signal,
    *,
    source_key: str,
    document_external_id: str,
    document_type: str,
    chunk_seq: int | None = None,
    chunk_start: int | None = None,
    chunk_end: int | None = None,
    chunk_labels: Sequence[str] = (),
) -> str:
    """Content/provenance identity independent of database IDs and ID-derived dedupe keys.

    Includes every scoring/summary input and grounded evidence so distinct extracted rows
    at the same source location have a deterministic tie break. Exact duplicate observations
    intentionally share a key and are reported by the planner, never ranked by surrogate ID.
    """
    content = {
        name: getattr(signal, name)
        for name in (
            "institution_code",
            "department",
            "title",
            "summary",
            "stage",
            "category",
            "observed_at",
            "budget_krw",
            "expected_year",
            "expected_half",
            "commitment",
            "keywords",
            "external_refs",
            "evidence",
        )
    }
    content["embedding"] = (
        [float(value) for value in signal.embedding] if signal.embedding is not None else None
    )
    return hashlib.sha256(
        _json(
            {
                "source": source_key,
                "document": document_external_id,
                "type": document_type,
                "chunk": [chunk_seq, chunk_start, chunk_end, list(chunk_labels)],
                "signal": content,
            }
        ).encode()
    ).hexdigest()


@dataclass(slots=True)
class PartitionGroup:
    index: int
    key: str
    summary: Opportunity
    members: list[Signal] = field(default_factory=list)
    # Opportunity IDs in these decisions are plan-local group indices, never persistent IDs.
    decisions: dict[int, LinkDecision] = field(default_factory=dict)
    ambiguous_keys: tuple[str, ...] = ()
    anchored_opportunity_id: int | None = None
    anchor_signal_ids: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class PartitionAnchor:
    """Original customer-facing evidence whose opportunity identity cannot be reassigned."""

    opportunity_id: int
    members: Sequence[Signal]


def _new_group(index: int, first: Signal, key: str) -> PartitionGroup:
    return PartitionGroup(
        index=index,
        key=key,
        summary=Opportunity(
            id=index,
            institution_code=first.institution_code,
            department=first.department,
            title=canonical_title(first.title),
            category=first.category,
            stage=first.stage,
            status="open",
            first_seen_at=first.observed_at,
            last_signal_at=first.observed_at,
        ),
    )


def _reference_decision(signal: Signal, groups: Sequence[PartitionGroup]) -> LinkDecision | None:
    if signal.institution_code is None:
        return None
    bids = _bid_numbers(signal.external_refs)
    if len(bids) > 1:
        raise _ReferenceConflictError
    refs = {key: signal.external_refs[key] for key in _REF_KEYS if signal.external_refs.get(key)}
    if not refs and not bids:
        return None
    matches, direct = [], []
    for group in groups:
        if group.summary.institution_code != signal.institution_code:
            continue
        has_match = has_direct = False
        for member in group.members:
            other = member.external_refs
            # Same JSON containment semantics as the SQL reference lookup: only singular
            # bid identities and an explicit one-item match in a plural list are targets.
            bid_match = any(
                other.get("bid_notice_no") == number
                or other.get("cancels_bid_notice_no") == number
                or number in (other.get("bid_notice_nos") or ())
                for number in bids
            )
            has_direct |= bid_match
            has_match |= bid_match or any(other.get(key) == value for key, value in refs.items())
        if has_match:
            matches.append(group.index)
        if has_direct:
            direct.append(group.index)
    target = reference_target(matches, direct)
    if target is None:
        return None
    group = groups[target]
    if not without_conflicting_numbers(
        signal,
        [group.summary],
        [(target, member.external_refs) for member in group.members],
    ):
        raise _ReferenceConflictError
    return LinkDecision(target, 1.0, "ref", False, {"ref": signal.external_refs})


def _similarity_decision(
    signal: Signal,
    groups: Sequence[PartitionGroup],
    *,
    threshold: float,
    band: float,
) -> LinkDecision | None:
    if signal.institution_code is None:
        return None
    candidates = [
        group.summary
        for group in groups
        if group.summary.institution_code == signal.institution_code
        and group.summary.last_signal_at >= signal.observed_at - timedelta(days=SPAN_DAYS)
        and group.summary.first_seen_at <= signal.observed_at + timedelta(days=SPAN_DAYS)
    ]
    candidates = without_conflicting_numbers(
        signal,
        candidates,
        [(opp.id, member.external_refs) for opp in candidates for member in groups[opp.id].members],
    )
    if signal.stage == "budget_line":
        candidates = without_other_budget_rows(
            signal,
            candidates,
            [
                (opp.id, member.document_id, member.title, member.department)
                for opp in candidates
                for member in groups[opp.id].members
                if member.stage == "budget_line"
            ],
        )
    return choose_similarity(signal, candidates, threshold=threshold, band=band)


def plan_partition(
    signals: Sequence[Signal],
    stable_keys: Mapping[int, str],
    *,
    threshold: float,
    review_band: float,
    today: date,
    calibration: dict[str, float] | None = None,
    anchors: Sequence[PartitionAnchor] = (),
) -> list[PartitionGroup]:
    """Reconcile the same observed universe to the same partition in one in-memory pass.

    The caller excludes strict protected opportunities and supplies accepted non-tentative
    members. Customer anchors are original immutable cores, not their prior automatic
    additions. Core decisions are absent from ``decisions`` and must not be rewritten when
    applying the plan. This function neither discovers pending signals nor sees future
    batches. Identical observations are reported instead of ranked by their database IDs.
    """
    universe = [*signals, *(member for anchor in anchors for member in anchor.members)]
    if len({signal.id for signal in universe}) != len(universe):
        raise ValueError("duplicate signal ID in planning universe")
    if len({anchor.opportunity_id for anchor in anchors}) != len(anchors):
        raise ValueError("duplicate anchored opportunity")
    if any(not anchor.members or anchor.opportunity_id <= 0 for anchor in anchors):
        raise ValueError("anchor requires an existing opportunity and original members")
    if any(len({s.institution_code for s in anchor.members}) != 1 for anchor in anchors):
        raise ValueError("anchor members must belong to one institution")
    if any(signal.verdict != "accepted" for signal in universe):
        raise ValueError("only accepted signals may be planned")
    if any(not stable_keys.get(signal.id) for signal in universe):
        raise ValueError("missing stable key for planning signal")
    counts = Counter(stable_keys[signal.id] for signal in universe)
    duplicate_keys = {key for key, count in counts.items() if count > 1}

    def order_key(signal: Signal) -> tuple[date, str]:
        return signal.observed_at, stable_keys[signal.id]

    ordered = sorted(signals, key=order_key)
    groups: list[PartitionGroup] = []
    for anchor in sorted(
        anchors, key=lambda anchor: tuple(sorted(order_key(member) for member in anchor.members))
    ):
        core = sorted(anchor.members, key=order_key)
        group = _new_group(len(groups), core[0], stable_keys[core[0].id])
        group.anchored_opportunity_id = anchor.opportunity_id
        group.anchor_signal_ids = frozenset(member.id for member in core)
        group.members.extend(core)
        group.ambiguous_keys = tuple(sorted({stable_keys[row.id] for row in core} & duplicate_keys))
        summarize_opportunity(group.summary, group.members, today=today, calibration=calibration)
        groups.append(group)
    for signal in ordered:
        try:
            decision = _reference_decision(signal, groups)
        except _ReferenceConflictError:
            decision = None  # An ambiguous reference must never fall back to similarity.
        else:
            if decision is None:
                decision = _similarity_decision(
                    signal, groups, threshold=threshold, band=review_band
                )
        if decision is None:
            index = len(groups)
            group = _new_group(index, signal, stable_keys[signal.id])
            groups.append(group)
            decision = LinkDecision(index, 1.0, "seed", False, {})
        else:
            group = groups[decision.opportunity_id]
        decision.reasons = decision.reasons | {
            "canonical_order": {"signal_key": stable_keys[signal.id], "group_key": group.key}
        }
        group.members.append(signal)
        # An automatic signal may precede an immutable core member by date or tie key.
        # Use the same complete member order as later database lifecycle refreshes.
        group.members.sort(key=order_key)
        group.decisions[signal.id] = decision
        group.ambiguous_keys = tuple(
            sorted({stable_keys[row.id] for row in group.members} & duplicate_keys)
        )
        summarize_opportunity(group.summary, group.members, today=today, calibration=calibration)
    return groups
