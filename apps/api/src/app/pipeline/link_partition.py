"""Canonical automatic partition planning, with no SQL or mutations of persisted inputs.

The caller supplies the complete eligible, already-linked universe for an institution and
excludes protected opportunities. This is a reconciliation plan, not a greedy new-arrival
decision: old automatic memberships are deliberately not read. Applying it and preserving
opportunity/customer identities belongs to the transaction layer.
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
) -> list[PartitionGroup]:
    """Reconcile the same observed universe to the same partition in one in-memory pass.

    Only automatic, accepted, non-tentative members may be supplied. The caller must exclude
    whole protected opportunities before planning. This function neither discovers pending
    signals nor includes unseen future batches. Duplicate stable keys are reported because
    identical observations cannot have a meaningful order among themselves.
    """
    if len({signal.id for signal in signals}) != len(signals):
        raise ValueError("duplicate signal ID in planning universe")
    if any(signal.verdict != "accepted" for signal in signals):
        raise ValueError("only accepted signals may be planned")
    if any(not stable_keys.get(signal.id) for signal in signals):
        raise ValueError("missing stable key for planning signal")
    counts = Counter(stable_keys[signal.id] for signal in signals)
    duplicate_keys = {key for key, count in counts.items() if count > 1}
    ordered = sorted(signals, key=lambda signal: (signal.observed_at, stable_keys[signal.id]))
    groups: list[PartitionGroup] = []
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
            summary = Opportunity(
                id=index,
                institution_code=signal.institution_code,
                department=signal.department,
                title=canonical_title(signal.title),
                category=signal.category,
                stage=signal.stage,
                status="open",
                first_seen_at=signal.observed_at,
                last_signal_at=signal.observed_at,
            )
            group = PartitionGroup(index=index, key=stable_keys[signal.id], summary=summary)
            groups.append(group)
            decision = LinkDecision(index, 1.0, "seed", False, {})
        else:
            group = groups[decision.opportunity_id]
        decision.reasons = decision.reasons | {
            "canonical_order": {"signal_key": stable_keys[signal.id], "group_key": group.key}
        }
        group.members.append(signal)
        group.decisions[signal.id] = decision
        group.ambiguous_keys = tuple(
            sorted({stable_keys[row.id] for row in group.members} & duplicate_keys)
        )
        summarize_opportunity(group.summary, group.members, today=today, calibration=calibration)
    return groups
