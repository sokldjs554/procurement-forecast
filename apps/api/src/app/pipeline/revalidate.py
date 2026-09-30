"""Recheck cached evidence without extraction, network calls, or deleting signal identities.

The default is a read-only, bounded audit. Apply requires the exact audit digest, takes
maintenance locks before reading, and uses a savepoint; the caller owns the final commit.
Resolved reviews and manual links are reported for a human rather than overwritten.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import now_utc, today_kst
from app.db.models import (
    CompanyProfile,
    Document,
    DocumentChunk,
    InstitutionRow,
    Opportunity,
    OpportunitySignal,
    Recommendation,
    ReviewItem,
    Signal,
    Source,
)
from app.domain.grounding import (
    EvidenceCheck,
    GroundingReport,
    locate_quote,
    official_evidence_issue,
    verify_extraction,
)
from app.domain.krw import detect_table_unit
from app.pipeline.link import refresh_opportunity
from app.pipeline.recommend import RANKER_VERSION, score_opportunity
from app.runtime import Runtime

REVALIDATION_VERSION = "stored-evidence-v1"
TEXT_DOCUMENT_TYPES = ("council_minutes", "budget_book")


class RevalidationPlanChangedError(ValueError):
    """The reviewed audit no longer describes the current source and derived data."""


@dataclass(slots=True)
class RevalidationResult:
    digest: str
    applied: bool
    scope: dict[str, Any]
    counts: dict[str, int]
    issues: dict[str, int]
    examples: list[dict[str, Any]]
    has_more: bool
    next_after_id: int | None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def validate_stored_signal(
    signal: Signal,
    document: Document,
    chunk: DocumentChunk | None,
    *,
    min_score: float = 88.0,
) -> GroundingReport:
    """Validate persisted claims at their source offsets, including full-document speakers.

    Raw budget/timing phrases were not persisted by older extractors. Only amounts and
    dates recoverable from the actual stored evidence can validate those old claims.
    Missing source text or offsets stays reviewable instead of being invented or fetched.
    """
    source = document.text or ""
    issues: list[str] = []
    checks: list[EvidenceCheck] = []
    evidence = signal.evidence or signal.grounding.get("evidence", [])
    for item in evidence:
        quote = item.get("quote") if isinstance(item, dict) else None
        if not isinstance(quote, str):
            issues.append("evidence_invalid")
            continue
        start, end = item.get("start"), item.get("end")
        if (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and 0 <= start < end <= len(source)
        ):
            check = locate_quote(source[start:end], quote, min_score=min_score)
            checks.append(
                EvidenceCheck(
                    quote,
                    check.found,
                    check.score,
                    start + check.start if check.start is not None else None,
                    start + check.end if check.end is not None else None,
                    check.method,
                )
            )
        else:
            # Grounding-report offsets are chunk-relative; only Signal.evidence has global
            # offsets. Do not silently promote a quote relocated to another speaker/section.
            issues.append("evidence_offsets_missing_or_invalid")
            checks.append(EvidenceCheck(quote, False, 0.0, None, None, "missing"))
    if not signal.evidence and evidence:
        issues.append("global_evidence_offsets_missing")
    if not source:
        issues.append("source_text_missing")
    if document.parse_status != "parsed":
        issues.append("source_not_parsed")
    if chunk is not None and (
        chunk.document_id != document.id
        or not 0 <= chunk.char_start < chunk.char_end <= len(source)
        or source[chunk.char_start : chunk.char_end] != chunk.text
    ):
        issues.append("source_chunk_mismatch")
    actual_quotes = [
        source[c.start : c.end]
        for c in checks
        if c.found and c.start is not None and c.end is not None
    ]
    report = verify_extraction(
        source="\n".join(actual_quotes),
        evidence_quotes=actual_quotes,
        budget_krw=signal.budget_krw,
        budget_text=None,
        expected_year=signal.expected_year if document.doc_type != "budget_book" else None,
        timing_text=None,
        reference_date=signal.observed_at,
        confidence=signal.confidence,
        min_score=min_score,
        default_unit=(detect_table_unit(source) or 1000)
        if document.doc_type == "budget_book"
        else 1,
    )
    report.evidence = checks
    if checks and any(c.found for c in checks) and not all(c.found for c in checks):
        issues.append("partial_evidence")
    if document.doc_type == "council_minutes":
        issue = official_evidence_issue(source, checks, char_start=0, commitment=signal.commitment)
        if issue:
            issues.append(issue)
    else:
        fiscal_year = document.structured.get("fiscal_year")
        if fiscal_year and signal.expected_year not in (None, fiscal_year):
            issues.append("fiscal_year_mismatch")
    if signal.institution_code is None:
        issues.append("institution_unresolved")
    if signal.stage != (
        "council_mention" if document.doc_type == "council_minutes" else "budget_line"
    ):
        issues.append("stage_mismatch")
    issues += [
        issue
        for issue in signal.grounding.get("issues", [])
        if isinstance(issue, str) and issue.startswith("degraded:")
    ]
    report.issues = sorted(set(report.issues + issues))
    report.verdict = (
        "rejected"
        if "evidence_not_found" in report.issues
        else "needs_review"
        if report.issues
        else "accepted"
    )
    return report


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "tolist"):
        return value.tolist()
    raise TypeError(f"Unsupported digest value: {type(value).__name__}")


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=_json_default).encode()
    ).hexdigest()


def _snapshot(row: Any) -> dict[str, Any]:
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


def _protected(review: ReviewItem | None, link: OpportunitySignal | None) -> bool:
    return bool(
        (
            review
            and (
                review.status != "open"
                or review.resolved_at
                or review.resolved_by
                or review.resolution
            )
        )
        or (link and link.method == "manual")
    )


async def revalidate_signals(
    session: AsyncSession,
    runtime: Runtime,
    *,
    apply: bool = False,
    expected_digest: str | None = None,
    limit: int = 1000,
    after_id: int = 0,
    source_keys: list[str] | None = None,
    doc_types: list[str] | None = None,
    today: date | None = None,
    example_limit: int = 20,
) -> RevalidationResult:
    """Audit accepted text signals; apply only a reviewed, unchanged plan, then caller commits.

    Pagination is by immutable signal ID. Use the same scope and business date for audit
    and apply. Previously rejected/reviewable signals, structured feeds, human resolutions,
    signal IDs, briefs, and notification/feedback history are preserved.
    """
    if session.new or session.dirty or session.deleted:
        raise ValueError(
            "Revalidation requires a clean session; flush or roll back pending changes"
        )
    if limit < 1 or limit > 10000 or after_id < 0 or example_limit < 0:
        raise ValueError("limit must be 1..10000; after_id and example_limit must be nonnegative")
    types = sorted(set(doc_types or TEXT_DOCUMENT_TYPES))
    if not set(types) <= set(TEXT_DOCUMENT_TYPES):
        raise ValueError("Only council_minutes and budget_book support stored-text revalidation")
    if apply and not expected_digest:
        raise ValueError("Apply requires expected_digest from a reviewed dry-run")
    scope: dict[str, Any] = {
        "version": REVALIDATION_VERSION,
        "limit": limit,
        "after_id": after_id,
        "source_keys": sorted(set(source_keys or [])),
        "doc_types": types,
        "today": (today or today_kst()).isoformat(),
        "min_score": runtime.settings.grounding_min_score,
    }
    # Prevent the caller's pending ORM changes from turning an audit into a write.
    with session.no_autoflush:
        if not apply:
            return await _run(session, scope, example_limit=example_limit)
    async with session.begin_nested():
        # Lock before recomputing the digest. Row locks alone do not prevent a new manual
        # review/link appearing between audit and apply. Timeout fails without partial writes.
        await session.execute(text("SET LOCAL lock_timeout = '5s'"))
        await session.execute(
            text(
                "LOCK TABLE sources, documents, document_chunks, signals, review_items, "
                "opportunity_signals, opportunities, recommendations, company_profiles, "
                "institutions IN SHARE ROW EXCLUSIVE MODE"
            )
        )
        return await _run(
            session, scope, example_limit=example_limit, expected_digest=expected_digest
        )


async def _run(
    session: AsyncSession,
    scope: dict[str, Any],
    *,
    example_limit: int,
    expected_digest: str | None = None,
) -> RevalidationResult:
    stmt = (
        select(Signal.id)
        .join(Document, Document.id == Signal.document_id)
        .where(
            Signal.verdict == "accepted",
            Signal.id > scope["after_id"],
            Document.doc_type.in_(scope["doc_types"]),
        )
        .order_by(Signal.id)
        .limit(scope["limit"] + 1)
    )
    if scope["source_keys"]:
        stmt = stmt.join(Source, Source.id == Document.source_id).where(
            Source.key.in_(scope["source_keys"])
        )
    ids = list((await session.scalars(stmt)).all())
    has_more = len(ids) > scope["limit"]
    ids = ids[: scope["limit"]]
    # Separate queries avoid transferring full source documents once per signal.
    signals = list(
        (
            await session.scalars(
                select(Signal).execution_options(populate_existing=True).where(Signal.id.in_(ids))
            )
        ).all()
    )
    signals.sort(key=lambda s: s.id)
    documents = {
        d.id: d
        for d in await session.scalars(
            select(Document)
            .execution_options(populate_existing=True)
            .where(Document.id.in_({s.document_id for s in signals}))
        )
    }
    chunks = {
        c.id: c
        for c in await session.scalars(
            select(DocumentChunk)
            .execution_options(populate_existing=True)
            .where(DocumentChunk.id.in_({s.chunk_id for s in signals}))
        )
    }
    reviews = {
        r.signal_id: r
        for r in await session.scalars(
            select(ReviewItem)
            .execution_options(populate_existing=True)
            .where(ReviewItem.signal_id.in_(ids))
        )
    }
    links = {
        link.signal_id: link
        for link in await session.scalars(
            select(OpportunitySignal)
            .execution_options(populate_existing=True)
            .where(OpportunitySignal.signal_id.in_(ids))
        )
    }
    reports = {
        s.id: validate_stored_signal(
            s,
            documents[s.document_id],
            chunks.get(s.chunk_id) if s.chunk_id is not None else None,
            min_score=scope["min_score"],
        )
        for s in signals
    }
    unsafe = [s for s in signals if reports[s.id].issues]
    protected = [s for s in unsafe if _protected(reviews.get(s.id), links.get(s.id))]
    protected_ids = {s.id for s in protected}
    changes = [s for s in unsafe if s.id not in protected_ids]
    removed = [links[s.id] for s in changes if s.id in links]
    opportunity_ids = sorted({link.opportunity_id for link in removed})
    opportunities = list(
        (
            await session.scalars(
                select(Opportunity)
                .execution_options(populate_existing=True)
                .where(Opportunity.id.in_(opportunity_ids))
            )
        ).all()
    )
    all_links = list(
        (
            await session.scalars(
                select(OpportunitySignal)
                .execution_options(populate_existing=True)
                .where(OpportunitySignal.opportunity_id.in_(opportunity_ids))
            )
        ).all()
    )
    survivors = list(
        (
            await session.scalars(
                select(Signal)
                .execution_options(populate_existing=True)
                .where(Signal.id.in_({link.signal_id for link in all_links}))
            )
        ).all()
    )
    recommendations = list(
        (
            await session.scalars(
                select(Recommendation)
                .execution_options(populate_existing=True)
                .where(Recommendation.opportunity_id.in_(opportunity_ids))
            )
        ).all()
    )
    profiles = {
        p.org_id: p
        for p in await session.scalars(
            select(CompanyProfile)
            .execution_options(populate_existing=True)
            .where(CompanyProfile.org_id.in_({r.org_id for r in recommendations}))
        )
    }
    regions = dict(
        (
            await session.execute(
                select(InstitutionRow.code, InstitutionRow.region_code).where(
                    InstitutionRow.code.in_({o.institution_code for o in opportunities})
                )
            )
        ).all()
    )
    fingerprint: dict[str, Any] = {
        "scope": scope,
        "has_more": has_more,
        "reports": {str(s.id): reports[s.id].to_json() for s in signals},
        "changes": [s.id for s in changes],
        "protected": sorted(protected_ids),
        "ranker_version": RANKER_VERSION,
        "documents": [
            {
                "id": d.id,
                "doc_type": d.doc_type,
                "source_id": d.source_id,
                "parse_status": d.parse_status,
                "text_hash": _digest(d.text),
                "fiscal_year": d.structured.get("fiscal_year"),
            }
            for d in sorted(documents.values(), key=lambda d: d.id)
        ],
        "chunks": [
            {
                "id": c.id,
                "document_id": c.document_id,
                "start": c.char_start,
                "end": c.char_end,
                "text_hash": _digest(c.text),
            }
            for c in sorted(chunks.values(), key=lambda c: c.id)
        ],
        "regions": regions,
    }
    for key, rows, order in (
        ("signals", signals, "id"),
        ("reviews", list(reviews.values()), "signal_id"),
        ("links", list(links.values()), "signal_id"),
        ("opportunities", opportunities, "id"),
        ("all_links", all_links, "signal_id"),
        ("survivors", survivors, "id"),
        ("profiles", list(profiles.values()), "org_id"),
    ):
        fingerprint[key] = [_snapshot(r) for r in sorted(rows, key=lambda r: getattr(r, order))]
    fingerprint["recommendations"] = [
        _snapshot(r) for r in sorted(recommendations, key=lambda r: (r.org_id, r.opportunity_id))
    ]
    digest = _digest(fingerprint)
    if expected_digest is not None and expected_digest != digest:
        raise RevalidationPlanChangedError("Data or scope changed; run and review a new dry-run")
    result = RevalidationResult(
        digest=digest,
        applied=expected_digest is not None,
        scope=scope,
        counts={
            "scanned": len(signals),
            "unchanged": len(signals) - len(unsafe),
            "needs_review": len(changes),
            "manual_review_required": len(protected),
            "links_removed": len(removed),
            "opportunities_refreshed": len(opportunities),
            "recommendations_refreshed": len(recommendations),
        },
        issues=dict(sorted(Counter(i for s in unsafe for i in reports[s.id].issues).items())),
        examples=[
            {
                "signal_id": s.id,
                "document_id": s.document_id,
                "title": s.title,
                "issues": reports[s.id].issues,
                "action": "manual_review_required" if s.id in protected_ids else "needs_review",
                "opportunity_id": links[s.id].opportunity_id if s.id in links else None,
            }
            for s in unsafe[:example_limit]
        ],
        has_more=has_more,
        next_after_id=ids[-1] if ids else None,
    )
    if expected_digest is None:
        return result
    timestamp = now_utc()
    for signal in changes:
        report = reports[signal.id]
        link = links.get(signal.id)
        signal.grounding = signal.grounding | {
            "revalidation": {
                "version": REVALIDATION_VERSION,
                "digest": digest,
                "at": timestamp.isoformat(),
                "previous_verdict": signal.verdict,
                "report": report.to_json(),
                "removed_link": json.loads(json.dumps(_snapshot(link), default=_json_default))
                if link
                else None,
            }
        }
        signal.verdict = "needs_review"
        review = reviews.get(signal.id)
        if review is None:
            session.add(ReviewItem(signal_id=signal.id, reasons=report.issues, status="open"))
        else:
            review.reasons = sorted(set(review.reasons + report.issues))
        if link is not None:
            await session.delete(link)
    await session.flush()
    business_date = date.fromisoformat(scope["today"])
    for opportunity in opportunities:
        # The canonical refresh preserves empty opportunities and excludes non-accepted
        # or tentative evidence, so all dependent identities/history remain intact.
        await refresh_opportunity(session, opportunity, today=business_date)
    await session.flush()
    await refresh_affected_recommendations(
        session, opportunity_ids, today=business_date, audit_digest=digest
    )
    from app.pipeline.link_reconcile import mark_link_dirty

    await mark_link_dirty(
        session, {o.institution_code for o in opportunities if o.institution_code}
    )
    return result


async def refresh_affected_recommendations(
    session: AsyncSession,
    opportunity_ids: list[int],
    *,
    today: date | None = None,
    audit_digest: str | None = None,
) -> int:
    """Rescore existing rows for refreshed opportunities, preserving human/notification history.

    The caller refreshes opportunity aggregates first and owns the transaction. No new
    recommendations, notifications, embeddings, or profile-wide deletions are produced.
    """
    if not opportunity_ids:
        return 0
    opportunities = {
        o.id: o
        for o in await session.scalars(
            select(Opportunity).where(Opportunity.id.in_(opportunity_ids))
        )
    }
    recommendations = list(
        (
            await session.scalars(
                select(Recommendation).where(Recommendation.opportunity_id.in_(opportunity_ids))
            )
        ).all()
    )
    profiles = {
        p.org_id: p
        for p in await session.scalars(
            select(CompanyProfile).where(
                CompanyProfile.org_id.in_({r.org_id for r in recommendations})
            )
        )
    }
    regions = dict(
        (
            await session.execute(
                select(InstitutionRow.code, InstitutionRow.region_code).where(
                    InstitutionRow.code.in_({o.institution_code for o in opportunities.values()})
                )
            )
        ).all()
    )
    business_date = today or today_kst()
    timestamp = now_utc()
    for recommendation in recommendations:
        opportunity = opportunities[recommendation.opportunity_id]
        profile = profiles.get(recommendation.org_id)
        if opportunity.status in ("open", "bid_open") and profile is not None:
            scored = score_opportunity(
                opportunity, profile, regions.get(opportunity.institution_code or ""), business_date
            )
            recommendation.score = scored.score
            recommendation.breakdown = scored.breakdown
        else:
            recommendation.score = 0.0
            recommendation.breakdown = recommendation.breakdown | {
                "revalidation": {"digest": audit_digest, "reason": "no_active_verified_opportunity"}
            }
        recommendation.ranker_version = RANKER_VERSION
        recommendation.computed_at = timestamp
        # Preserve feedback, feedback_at, notified_stage, and the recommendation row itself.
    await session.flush()
    return len(recommendations)
