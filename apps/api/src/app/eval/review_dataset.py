"""Frozen, source-backed human review samples, separate from machine or field-level gold.

Only parsed council/budget text is supported. Rejected candidates remain useful negatives
when their quoted source exists, even if their claims fail deterministic grounding. Positive
human judgements with insufficient grounding are counted as exclusions, never promoted.

Exports default to 500 reviewed candidates. Query results/source objects remain in memory;
digest calculation and atomic JSONL writing use one serialized sample at a time. Explicit
``limit=None`` opts into an unbounded snapshot. Each sample retains its full parsed source.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tempfile
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.db.models import Document, DocumentChunk, ReviewItem, Signal, Source
from app.pipeline.revalidate import TEXT_DOCUMENT_TYPES, validate_stored_signal

SCHEMA_VERSION = "review-dataset-v1"
RESOLVED_STATUSES = ("approved", "edited", "rejected")
EDIT_FIELDS = ("title", "category", "budget_krw", "expected_year", "commitment", "institution_code")
CLAIM_FIELDS = (
    "title",
    "summary",
    "category",
    "keywords",
    "budget_krw",
    "expected_year",
    "expected_half",
    "commitment",
    "procurement_type",
    "institution_code",
    "stage",
)
_SOURCE_ISSUES = {
    "evidence_invalid",
    "evidence_not_found",
    "partial_evidence",
    "source_text_missing",
    "source_not_parsed",
    "source_chunk_mismatch",
    "global_evidence_offsets_missing",
    "evidence_offsets_missing_or_invalid",
}


@dataclass(frozen=True, slots=True)
class ReviewedSignal:
    signal: Signal
    document: Document
    review: ReviewItem
    chunk: DocumentChunk | None = None


@dataclass(slots=True)
class ReviewDatasetReport:
    schema_version: str
    digest: str
    candidate_reviews: int
    exported: int
    excluded: int
    excluded_reasons: dict[str, int]
    split_counts: dict[str, int]
    source_documents: int
    split_seed: str
    has_more: bool
    truncated: bool
    scope: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ReviewDataset:
    records: list[dict[str, Any]]
    report: ReviewDatasetReport

    def iter_jsonl_bytes(self) -> Iterator[bytes]:
        manifest = self.report.to_json() | {
            "record_type": "review_dataset_manifest",
            "digest_scope": "sample_lines",
            "temporal_scope": "current_stored_snapshot",
            "gold_standard": False,
            "split_policy": "connected_document_content_text_hashes_80_10_10",
            "split_assignment_scope": "this_frozen_export",
            "coverage": "reviewed_candidates_only_not_exhaustive_document_labels",
        }
        yield _line(manifest)
        for record in self.records:
            yield _line(record)

    def jsonl_bytes(self) -> bytes:
        return b"".join(self.iter_jsonl_bytes())


def _line(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _timestamp(value: Any) -> str | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC).isoformat()


def _actor(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _changes(resolution: dict[str, Any]) -> dict[str, Any]:
    changes = resolution.get("changes", {})
    if not isinstance(changes, dict):
        return {}
    clean = {}
    for field in EDIT_FIELDS:
        values = changes.get(field)
        if not isinstance(values, dict):
            continue
        # Never copy arbitrary resolution JSON, freeform notes, links, or user profiles.
        allowed_type = int if field in ("budget_krw", "expected_year") else str
        if all(
            v is None or type(v) is allowed_type for v in (values.get("from"), values.get("to"))
        ):
            clean[field] = {"from": values.get("from"), "to": values.get("to")}
    return clean


def _history(resolution: dict[str, Any]) -> list[dict[str, Any]]:
    history = resolution.get("history", [])
    if not isinstance(history, list):
        return []
    return [
        {
            "action": item.get("action")
            if item.get("action") in ("approve", "edit", "reject")
            else None,
            "actor_user_id": _actor(item.get("resolved_by")),
            "resolved_at": _timestamp(item.get("resolved_at")),
            "changes": _changes(item),
        }
        for item in history
        if isinstance(item, dict)
    ]


def _sample(case: ReviewedSignal, min_score: float) -> tuple[dict[str, Any] | None, list[str]]:
    signal, document, review = case.signal, case.document, case.review
    issues: list[str] = []
    if review.status not in RESOLVED_STATUSES:
        issues.append("review_not_resolved")
    if _timestamp(review.resolved_at) is None:
        issues.append("review_timestamp_missing")
    expected_verdict = "rejected" if review.status == "rejected" else "accepted"
    if signal.verdict != expected_verdict:
        issues.append("review_signal_verdict_mismatch")
    if review.signal_id != signal.id or signal.document_id != document.id:
        issues.append("review_source_identity_mismatch")
    action = "reject" if review.status == "rejected" else "approve"
    allowed_actions = {"reject"} if review.status == "rejected" else {"approve", "edit"}
    if review.resolution.get("action") not in allowed_actions:
        issues.append("review_action_mismatch")
    if document.doc_type not in TEXT_DOCUMENT_TYPES:
        issues.append("unsupported_document_type")
    if not document.text:
        issues.append("source_text_missing")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", document.content_hash or ""):
        issues.append("source_content_hash_invalid")
    changes = _changes(review.resolution)
    if any(getattr(signal, field) != change["to"] for field, change in changes.items()):
        issues.append("review_edit_mismatch")
    if issues:
        return None, sorted(set(issues))
    report = validate_stored_signal(signal, document, case.chunk, min_score=min_score)
    issues += sorted(_SOURCE_ISSUES.intersection(report.issues))
    # A human can resolve low model confidence; they cannot create missing source evidence.
    # Keep the original machine warning in the sample for evaluation and disagreement analysis.
    blocking_issues = set(report.issues) - {"low_confidence"}
    if review.status != "rejected" and blocking_issues:
        issues.append("positive_grounding_insufficient")
    if issues:
        return None, sorted(set(issues))
    assert document.text is not None
    history = _history(review.resolution)
    evidence = [
        {
            "quote": check.quote,
            "source_quote": document.text[check.start : check.end],
            "start": check.start,
            "end": check.end,
            "method": check.method,
            "score": check.score,
        }
        for check in report.evidence
    ]
    grounding = report.to_json()
    grounding.pop("evidence")
    grounding["issues"] = [
        "degraded_source" if issue.startswith("degraded:") else issue for issue in report.issues
    ]
    return {
        "record_type": "human_review_sample",
        "schema_version": SCHEMA_VERSION,
        "review_id": review.id,
        "signal_id": signal.id,
        "source": {
            "document_id": document.id,
            "doc_type": document.doc_type,
            "published_at": document.published_at.isoformat(),
            "observed_at": signal.observed_at.isoformat(),
            "representation": "stored_parsed_text",
            "text": document.text,
            "text_sha256": _sha(document.text),
            "ingested_content_sha256": document.content_hash.lower(),
            "raw_content_reverified": False,
        },
        "candidate": {field: getattr(signal, field) for field in CLAIM_FIELDS},
        "evidence": evidence,
        "machine_grounding": grounding,
        "human_label": {
            "origin": "human_review",
            "status": review.status,
            "decision": action,
            "decision_scope": "signal_acceptance_with_recorded_edits",
            "reviewed_fields": sorted(changes),
            "gold_standard": False,
            "revision": len(history) + 1,
            "actor_user_id": _actor(review.resolved_by),
            "actor_unavailable": _actor(review.resolved_by) is None,
            "resolved_at": _timestamp(review.resolved_at),
            "changes": changes,
            "history": history,
        },
    }, []


def _assign_splits(records: list[dict[str, Any]], split_seed: str) -> None:
    """Connected components keep aliases sharing document, raw hash OR text hash together."""
    parent: dict[str, str] = {}

    def find(key: str) -> str:
        parent.setdefault(key, key)
        root = key
        while parent[root] != root:
            root = parent[root]
        while parent[key] != key:
            previous = parent[key]
            parent[key] = root
            key = previous
        return root

    def keys(record: dict[str, Any]) -> tuple[str, str, str]:
        source = record["source"]
        return (
            f"document:{source['document_id']}",
            f"raw:{source['ingested_content_sha256']}",
            f"text:{source['text_sha256']}",
        )

    for record in records:
        group = keys(record)
        root = find(group[0])
        for key in group[1:]:
            parent[find(key)] = root
    hashes: dict[str, list[str]] = {}
    for key in list(parent):
        if key.startswith(("raw:", "text:")):
            hashes.setdefault(find(key), []).append(key)
    for record in records:
        group_hash = _sha(min(hashes[find(keys(record)[0])]))
        bucket = int(_sha(f"{split_seed}\0{group_hash}")[:8], 16) % 100
        record["split_group"] = group_hash
        record["split"] = "train" if bucket < 80 else "validation" if bucket < 90 else "test"


def assemble_review_dataset(
    cases: Sequence[ReviewedSignal],
    *,
    split_seed: str = "review-v1",
    min_score: float = 88.0,
    has_more: bool = False,
    scope: dict[str, Any] | None = None,
) -> ReviewDataset:
    """Build deterministic records without changing any ORM object or promoting whole documents."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", split_seed):
        raise ValueError(
            "split_seed must contain 1..64 ASCII letters, numbers, dot, dash or underscore"
        )
    if not 88.0 <= min_score <= 100.0:
        raise ValueError("min_score must be between 88 and 100")
    records = []
    excluded = 0
    reasons: Counter[str] = Counter()
    for case in sorted(cases, key=lambda case: case.review.id):
        record, issues = _sample(case, min_score)
        if record is None:
            excluded += 1
            reasons.update(issues)
        else:
            records.append(record)
    _assign_splits(records, split_seed)
    digest = hashlib.sha256()
    for record in records:
        digest.update(_line(record))
    splits = Counter(record["split"] for record in records)
    report = ReviewDatasetReport(
        schema_version=SCHEMA_VERSION,
        digest=digest.hexdigest(),
        candidate_reviews=len(cases),
        exported=len(records),
        excluded=excluded,
        excluded_reasons=dict(sorted(reasons.items())),
        split_counts={key: splits[key] for key in ("train", "validation", "test")},
        source_documents=len({record["source"]["document_id"] for record in records}),
        split_seed=split_seed,
        has_more=has_more,
        truncated=has_more,
        scope=scope or {"min_score": min_score},
    )
    return ReviewDataset(records, report)


def _write_atomic(path: Path, dataset: ReviewDataset) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as f:
            temporary = Path(f.name)
            f.writelines(dataset.iter_jsonl_bytes())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def export_review_dataset(
    session: AsyncSession,
    path: Path,
    *,
    source_keys: list[str] | None = None,
    doc_types: list[str] | None = None,
    limit: int | None = 500,
    split_seed: str = "review-v1",
    min_score: float = 88.0,
    resolved_before: datetime | None = None,
) -> ReviewDatasetReport:
    """Atomically write a reviewed-candidate snapshot; no DB mutation, LLM or external call.

    ``resolved_before`` restricts the latest recorded judgement; it does not reconstruct
    historical source text or previous review states. A limit produces an explicit partial set.
    """
    if session.new or session.dirty or session.deleted:
        raise ValueError("Export requires a clean session; flush or roll back pending changes")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    if resolved_before is not None and _timestamp(resolved_before) is None:
        raise ValueError("resolved_before must be timezone-aware")
    # One statement ensures source, candidate and review are from one PostgreSQL snapshot.
    stmt = (
        select(ReviewItem, Signal, Document, DocumentChunk)
        .join(Signal, Signal.id == ReviewItem.signal_id)
        .join(Document, Document.id == Signal.document_id)
        .outerjoin(DocumentChunk, DocumentChunk.id == Signal.chunk_id)
        .where(ReviewItem.status.in_(RESOLVED_STATUSES))
        .order_by(ReviewItem.id)
        .options(defer(Signal.embedding))
        .execution_options(populate_existing=True)
    )
    if source_keys:
        stmt = stmt.join(Source, Source.id == Document.source_id).where(Source.key.in_(source_keys))
    if doc_types:
        stmt = stmt.where(Document.doc_type.in_(doc_types))
    if resolved_before is not None:
        stmt = stmt.where(ReviewItem.resolved_at <= resolved_before)
    if limit is not None:
        stmt = stmt.limit(limit + 1)
    with session.no_autoflush:
        rows = (await session.execute(stmt)).all()
    has_more = limit is not None and len(rows) > limit
    if limit is not None:
        rows = rows[:limit]
    dataset = assemble_review_dataset(
        [ReviewedSignal(signal=s, document=d, review=r, chunk=c) for r, s, d, c in rows],
        split_seed=split_seed,
        min_score=min_score,
        has_more=has_more,
        scope={
            "source_keys": sorted(set(source_keys or [])),
            "doc_types": sorted(set(doc_types or [])),
            "limit": limit,
            "resolved_before": _timestamp(resolved_before),
            "min_score": min_score,
        },
    )
    await asyncio.to_thread(_write_atomic, Path(path), dataset)
    return dataset.report
