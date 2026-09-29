"""Process one document: parse → chunk → triage → extract → verify → resolve → embed → persist.

Idempotent: re-processing a document (content changed, parser improved, prompt bumped) replaces
its chunks and signals; opportunity aggregates are recomputed by the linker afterwards.
Documents with human review or manual link decisions require an explicit migration instead.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import today_kst
from app.db.models import (
    Document,
    DocumentChunk,
    Opportunity,
    OpportunitySignal,
    ReviewItem,
    Signal,
)
from app.domain.grounding import GroundingReport, verify_extraction
from app.domain.krw import detect_table_unit
from app.domain.stages import CANCEL_NOTICE, CANCELS_KEY, Stage
from app.domain.synonyms import canonicalize
from app.domain.taxonomy import Category, classify_category
from app.domain.text import collapse_ws
from app.llm.prompts import ChunkContext
from app.llm.schemas import ExtractedSignal
from app.log import get_logger
from app.parsing.chunking import Chunk, chunk_document
from app.parsing.dispatch import parse_content
from app.pipeline.triage import triage_chunk
from app.runtime import Runtime
from app.storage import load_raw

log = get_logger(__name__)

_STAGE_BY_DOC = {
    "council_minutes": Stage.COUNCIL,
    "budget_book": Stage.BUDGET,
    "order_plan": Stage.ORDER_PLAN,
    "prespec": Stage.PRESPEC,
    "bid_notice": Stage.BID,
    "award": Stage.AWARD,
}
_TITLE_NOISE_RE = re.compile(
    r"^\s*(?:\[[^\]]{1,10}\]\s*)*|\s*\((?:협상에\s*의한\s*계약|긴급|재공고|수정공고)[^)]*\)\s*$"
)
_TITLE_YEAR_RE = re.compile(r"^\s*'?(?:20)?\d{2}\s*년(?:도)?\s*")


def canonical_title(title: str) -> str:
    """'[긴급] 2027년 스마트쉘터 구축사업 (협상에 의한 계약)' → '스마트쉘터 구축사업'."""
    t = _TITLE_NOISE_RE.sub("", collapse_ws(title))
    t = _TITLE_YEAR_RE.sub("", t)
    return _TITLE_NOISE_RE.sub("", t).strip()


@dataclass(slots=True)
class ProcessResult:
    document_id: int
    chunks: int = 0
    triaged: int = 0
    signal_ids: list[int] = field(default_factory=list)
    needs_review: int = 0
    rejected: int = 0
    degraded: int = 0
    extractor: str | None = None


class ReprocessingProtectedError(RuntimeError):
    """Replacing a document's derivations would erase persisted human decisions."""


async def protect_human_decisions(session: AsyncSession, document_id: int) -> None:
    """Refuse destructive source/derivation changes; caller holds the Document row lock."""
    from app.pipeline.relations import has_relation_review_for_document

    # Review writers use the same Signal-first lock order. A separate query after acquiring
    # the locks observes any review committed while we waited (READ COMMITTED isolation).
    signal_ids = list(
        await session.scalars(
            select(Signal.id)
            .where(Signal.document_id == document_id)
            .order_by(Signal.id)
            .with_for_update()
        )
    )
    if not signal_ids:
        if await has_relation_review_for_document(session, document_id):
            raise ReprocessingProtectedError(
                f"document {document_id} has human decisions in relation history; "
                "source replacement requires an explicit migration"
            )
        return
    protected = await session.scalar(
        select(Signal.id)
        .outerjoin(ReviewItem, ReviewItem.signal_id == Signal.id)
        .outerjoin(OpportunitySignal, OpportunitySignal.signal_id == Signal.id)
        .where(
            Signal.id.in_(signal_ids),
            or_(
                ReviewItem.status != "open",
                ReviewItem.resolved_at.is_not(None),
                ReviewItem.resolved_by.is_not(None),
                ReviewItem.resolution != {},
                OpportunitySignal.method == "manual",
            ),
        )
        .limit(1)
    )
    if protected is not None or await has_relation_review_for_document(session, document_id):
        raise ReprocessingProtectedError(
            f"document {document_id} has human decisions; re-extraction requires an explicit "
            "review/manual-link/relation migration"
        )


def _dedupe_key(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()


def _observed_at(doc: Document) -> date:
    meeting = doc.structured.get("meeting_date")
    return date.fromisoformat(meeting) if meeting else doc.published_at


def _demand_owner(runtime: Runtime, doc: Document) -> str | None:
    if doc.institution_code is None:
        return None
    inst = runtime.registry.get(doc.institution_code)
    return inst.demand_owner_code if inst else doc.institution_code


async def _parse(session: AsyncSession, runtime: Runtime, doc: Document) -> None:
    content = await load_raw(doc.raw_uri) if doc.raw_uri else None
    parsed = await parse_content(
        mime=doc.mime,
        content=content,
        doc_type=doc.doc_type,
        title=doc.title,
        publisher=doc.publisher_raw,
        structured=doc.structured,
        ocr=runtime.ocr,
        corrector=runtime.corrector,
        min_chars_per_page=runtime.settings.ocr_min_text_chars_per_page,
    )
    doc.text = parsed.text
    doc.parse_method = parsed.method
    doc.text_quality = parsed.quality
    doc.structured = doc.structured | {"pages": parsed.pages, "ocr_pages": parsed.ocr_pages}


def _structured_signal(runtime: Runtime, doc: Document) -> dict[str, Any]:
    s = doc.structured
    title = canonical_title(doc.title) or doc.title
    category, _ = classify_category(title)
    amount = s.get("amount_krw") if isinstance(s.get("amount_krw"), int) else None
    year = s.get("order_year") or doc.published_at.year
    month = s.get("order_month")
    half = ("H1" if int(month) <= 6 else "H2") if month else None
    refs = {k: s[k] for k in ("order_plan_no", "prespec_no", "bid_notice_no") if s.get(k)}
    if s.get("bid_notice_nos"):
        refs["bid_notice_nos"] = s["bid_notice_nos"]
    cancels = s.get("notice_kind") == CANCEL_NOTICE and s.get("bid_notice_no")
    if cancels:
        refs[CANCELS_KEY] = s["bid_notice_no"]  # this 차수 withdraws 공고 <no>
    owner = _demand_owner(runtime, doc)
    issues = [] if owner else ["institution_unresolved"]
    return {
        "stage": _STAGE_BY_DOC[doc.doc_type].value,
        "institution_code": owner,
        "speaker_institution_code": doc.institution_code,
        "department": doc.department,
        "title": title,
        "summary": f"{doc.publisher_raw or ''} {doc.title}{' (취소공고)' if cancels else ''}".strip(),
        "category": category.value,
        "keywords": [title],
        "budget_krw": amount,
        "expected_year": int(year) if year else None,
        "expected_half": half,
        "commitment": "committed",
        "procurement_type": None,
        "confidence": 0.99 if owner else 0.6,
        "evidence": [
            {
                "quote": doc.text,
                "found": True,
                "score": 100.0,
                "start": 0,
                "end": len(doc.text or ""),
                "method": "exact",
            }
        ],
        "grounding": {"issues": issues, "source": "structured"},
        "verdict": "accepted" if owner else "needs_review",
        "extractor": "structured-v1",
        "observed_at": doc.published_at,
        "external_refs": refs,
        "dedupe_key": _dedupe_key(doc.id, "record", doc.content_hash),
    }


@dataclass(slots=True)
class CheckedSignal:
    """A model's signal after the grounding verifier — what the pipeline actually stores."""

    budget_krw: int | None
    expected_year: int | None
    report: GroundingReport


def check_signal(
    sig: ExtractedSignal,
    *,
    text: str,
    doc_type: str,
    reference_date: date,
    fiscal_year: int | None,
    table_unit: int,
    min_score: float,
    document_text: str | None = None,
    char_start: int = 0,
) -> CheckedSignal:
    expected_year = sig.expected_year
    if doc_type == "budget_book" and fiscal_year:
        expected_year = expected_year or fiscal_year
    report = verify_extraction(
        source=text,
        evidence_quotes=sig.evidence,
        budget_krw=sig.budget_krw,
        budget_text=sig.budget_text,
        expected_year=expected_year if doc_type != "budget_book" else None,
        timing_text=sig.timing_text,
        reference_date=reference_date,
        confidence=sig.confidence,
        min_score=min_score,
        default_unit=table_unit if doc_type == "budget_book" else 1,
        council_document=(document_text if document_text is not None else text)
        if doc_type == "council_minutes"
        else None,
        char_start=char_start,
        commitment=sig.commitment,
    )
    budget = sig.budget_krw
    if report.budget_grounded is False and report.budget_parsed:
        budget = report.budget_parsed  # the parser, not the model, has the last word on numbers
    return CheckedSignal(budget, expected_year, report)


def _text_signal(
    runtime: Runtime,
    doc: Document,
    chunk: Chunk,
    sig: ExtractedSignal,
    *,
    extractor: str,
    degraded_reason: str | None,
    fiscal_year: int | None,
    table_unit: int,
) -> dict[str, Any]:
    observed = _observed_at(doc)
    checked = check_signal(
        sig,
        text=chunk.text,
        doc_type=doc.doc_type,
        reference_date=observed,
        fiscal_year=fiscal_year,
        table_unit=table_unit,
        min_score=runtime.settings.grounding_min_score,
        document_text=doc.text,
        char_start=chunk.char_start,
    )
    report = checked.report
    owner = _demand_owner(runtime, doc)
    if sig.institution_mention and doc.doc_type == "council_minutes":
        mentioned = runtime.registry.resolve(sig.institution_mention)
        # A council can discuss another body's project (e.g. 교육청); keep the explicit one.
        if mentioned.institution and mentioned.institution.kind != "council":
            owner = mentioned.institution.demand_owner_code
    if owner is None:
        report.issues.append("institution_unresolved")
    if degraded_reason:
        report.issues.append(f"degraded:{degraded_reason}")
    verdict = report.verdict
    if verdict == "accepted" and report.issues:
        verdict = "needs_review"
    evidence = [
        {
            "quote": e.quote,
            "found": e.found,
            "score": e.score,
            "start": chunk.char_start + e.start if e.start is not None else None,
            "end": chunk.char_start + e.end if e.end is not None else None,
            "method": e.method,
        }
        for e in report.evidence
    ]
    return {
        "stage": _STAGE_BY_DOC[doc.doc_type].value,
        "institution_code": owner,
        "speaker_institution_code": doc.institution_code,
        "department": sig.department or doc.department,
        "title": sig.title,
        "summary": sig.summary,
        "category": sig.category.value if isinstance(sig.category, Category) else str(sig.category),
        "keywords": sig.keywords,
        "budget_krw": checked.budget_krw,
        "expected_year": checked.expected_year,
        "expected_half": sig.expected_half,
        "commitment": sig.commitment,
        "procurement_type": sig.procurement_type,
        "confidence": sig.confidence,
        "evidence": evidence,
        "grounding": report.to_json() | {"issues": report.issues},
        "verdict": verdict,
        "extractor": extractor,
        "observed_at": observed,
        "external_refs": {},
        "dedupe_key": _dedupe_key(doc.id, chunk.seq, collapse_ws(sig.title), sig.commitment),
    }


async def process_document(
    session: AsyncSession, runtime: Runtime, document_id: int, *, final_attempt: bool = True
) -> ProcessResult:
    # PostgreSQL NO KEY UPDATE serializes source edits/reprocessing while permitting the
    # independent paid-call/cache transaction's foreign-key KEY SHARE on this document.
    doc = await session.get(
        Document, document_id, with_for_update={"key_share": True}, populate_existing=True
    )
    if doc is None:
        raise LookupError(f"document {document_id} not found")
    await protect_human_decisions(session, document_id)
    result = ProcessResult(document_id)
    try:
        if doc.text is None:
            await _parse(session, runtime, doc)
    except Exception as exc:
        doc.parse_status = "failed"
        doc.parse_error = f"{type(exc).__name__}: {exc}"[:2000]
        raise
    text = doc.text or ""

    # Replace previous derivations of this document.
    old_opportunity_ids = list(
        await session.scalars(
            select(OpportunitySignal.opportunity_id)
            .join(Signal, Signal.id == OpportunitySignal.signal_id)
            .where(Signal.document_id == doc.id)
            .distinct()
        )
    )
    await session.execute(delete(Signal).where(Signal.document_id == doc.id))
    await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
    await session.flush()

    chunks = chunk_document(doc.doc_type, text)
    result.chunks = len(chunks)
    rows: list[dict[str, Any]] = []
    chunk_rows: dict[int, DocumentChunk] = {}
    fiscal_year = doc.structured.get("fiscal_year")
    table_unit = detect_table_unit(text) or 1000
    institution_label = None
    if doc.institution_code and (inst := runtime.registry.get(doc.institution_code)):
        institution_label = inst.name

    for chunk in chunks:
        tri = triage_chunk(chunk.text, kind=chunk.kind, threshold=runtime.settings.triage_threshold)
        row = DocumentChunk(
            document_id=doc.id,
            seq=chunk.seq,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            text=chunk.text,
            labels=chunk.labels,
            triage_score=tri.score,
            triage_passed=tri.passed,
        )
        session.add(row)
        chunk_rows[chunk.seq] = row
        if not tri.passed:
            continue
        result.triaged += 1
        if chunk.kind == "record":
            rows.append(
                _structured_signal(
                    runtime,
                    doc,
                )
                | {"_chunk": chunk.seq}
            )
            result.extractor = "structured-v1"
            continue
        ctx = ChunkContext(
            doc_type=doc.doc_type,
            title=doc.title,
            institution=institution_label,
            document_date=_observed_at(doc),
            labels=chunk.labels,
            text=chunk.text,
            fiscal_year=fiscal_year,
        )
        attempt = await runtime.llm.extract(
            session, ctx, document_id=doc.id, final_attempt=final_attempt
        )
        result.extractor = attempt.extractor
        if attempt.degraded:
            result.degraded += 1
        for sig in attempt.output.signals:
            rows.append(
                _text_signal(
                    runtime,
                    doc,
                    chunk,
                    sig,
                    extractor=attempt.extractor,
                    degraded_reason=attempt.reason if attempt.degraded else None,
                    fiscal_year=fiscal_year,
                    table_unit=table_unit,
                )
                | {"_chunk": chunk.seq}
            )
    await session.flush()

    # Deduplicate within the document (the same project can be named in two exchanges).
    unique: dict[str, dict[str, Any]] = {}
    for candidate in rows:
        unique.setdefault(candidate["dedupe_key"], candidate)
    rows = list(unique.values())

    if rows:
        texts = [
            canonicalize(f"{r['title']} {r['summary']} {' '.join(r['keywords'])}") for r in rows
        ]
        vectors = await runtime.embedder.embed(texts)
        for fields, vec in zip(rows, vectors, strict=True):
            chunk_seq = fields.pop("_chunk")
            signal = Signal(
                document_id=doc.id, chunk_id=chunk_rows[chunk_seq].id, embedding=vec, **fields
            )
            session.add(signal)
            await session.flush()
            result.signal_ids.append(signal.id)
            if signal.verdict == "needs_review":
                result.needs_review += 1
                session.add(
                    ReviewItem(signal_id=signal.id, reasons=signal.grounding.get("issues", []))
                )
            elif signal.verdict == "rejected":
                result.rejected += 1

    doc.parse_status = "parsed"
    doc.parse_error = None
    doc.extracted_at = datetime.now(UTC)
    await session.flush()
    if old_opportunity_ids:
        # The new signals may later link elsewhere. Retract the old aggregates now, in the
        # caller's transaction, keeping opportunity and customer-history identities intact.
        from app.pipeline.link import refresh_opportunity
        from app.pipeline.revalidate import refresh_affected_recommendations

        business_date = today_kst()
        old_opportunities = await session.scalars(
            select(Opportunity)
            .where(Opportunity.id.in_(old_opportunity_ids))
            .order_by(Opportunity.id)
            .with_for_update()
        )
        old_owners: set[str] = set()
        for opportunity in old_opportunities:
            await refresh_opportunity(session, opportunity, today=business_date)
            if opportunity.institution_code:
                old_owners.add(opportunity.institution_code)
        await session.flush()
        await refresh_affected_recommendations(session, old_opportunity_ids, today=business_date)
        from app.pipeline.link_reconcile import mark_link_dirty

        await mark_link_dirty(session, old_owners)
    log.info(
        "document.processed",
        document_id=doc.id,
        doc_type=doc.doc_type,
        chunks=result.chunks,
        triaged=result.triaged,
        signals=len(result.signal_ids),
        needs_review=result.needs_review,
        rejected=result.rejected,
        degraded=result.degraded,
    )
    return result


async def pending_document_ids(session: AsyncSession, limit: int = 500) -> list[int]:
    # Publication order: signals are created, and then linked, in the order the provider put the
    # records out, so a 발주계획 is seen before the 공고 that follows it (backfill.process_pending
    # links in slices and relies on this).
    rows = await session.scalars(
        select(Document.id)
        .where(Document.parse_status == "pending")
        .order_by(Document.published_at)
        .limit(limit)
    )
    return list(rows)
