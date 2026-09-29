"""ORM → DTO assembly, including the human-readable "why this is recommended" reasons."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    DocumentRef,
    EvidenceOut,
    InstitutionRef,
    LinkOut,
    OpportunityCard,
    SignalOut,
)
from app.db.models import (
    Document,
    DocumentChunk,
    InstitutionRow,
    Opportunity,
    OpportunitySignal,
    Recommendation,
    Signal,
)
from app.domain.stages import CANCELS_KEY, STAGE_LABEL, STAGE_ORDER, Stage, tender_is_out
from app.domain.taxonomy import CATEGORIES, Category
from app.domain.timing import remaining_window

_COMMITMENT_KO = {
    "committed": "의회에서 '반영하겠다'고 답함",
    "planned": "의회에서 추진 계획을 밝힘",
    "reviewing": "의회에서 '검토하겠다'고 답함",
    "declined": "의회에서 '어렵다'고 답함",
}


def category_label(key: str) -> str:
    try:
        return CATEGORIES[Category(key)].label
    except ValueError:
        return key


def explain(opp: Opportunity, breakdown: dict[str, Any] | None) -> list[str]:
    if opp.signal_count == 0:
        return ["유효한 근거가 없어 검토가 필요합니다"]
    reasons: list[str] = []
    if breakdown:
        hits = breakdown.get("keyword_hits") or []
        if hits:
            reasons.append("키워드: " + ", ".join(hits[:3]))
        feats = breakdown.get("features", {})
        if feats.get("category") == 1.0:
            reasons.append(f"관심 분야: {category_label(opp.category)}")
        if feats.get("region") == 1.0:
            reasons.append("관심 지역 사업")
        if feats.get("budget") == 1.0 and opp.est_budget_krw:
            reasons.append("원하는 사업 규모")
        days = breakdown.get("days_to_window")
        if isinstance(days, int) and days > 0 and opp.status == "open":
            months = max(1, round(days / 30))
            reasons.append(f"입찰까지 {months}개월쯤 남음")
    stage = Stage(opp.stage)
    if stage is Stage.COUNCIL and opp.best_commitment:
        reasons.append(_COMMITMENT_KO.get(opp.best_commitment, opp.best_commitment))
    elif STAGE_ORDER[stage] >= STAGE_ORDER[Stage.BUDGET] and stage is not Stage.BID:
        reasons.append(f"{STAGE_LABEL[stage]}까지 확인됨")
    if opp.status == "bid_open":
        reasons.append("입찰 진행 중")
    return reasons


def shown_window(opp: Opportunity, today: date) -> tuple[date | None, date | None, bool]:
    return remaining_window(
        opp.bid_window_start, opp.bid_window_end, today, published=opp.bid_published_at
    )


def lead_days(opp: Opportunity, today: date) -> int | None:
    """Days until the forecast window opens; 0 inside it; None once it has passed or the
    tender is out."""
    start, _, passed = shown_window(opp, today)
    if opp.bid_published_at or start is None or passed:
        return None
    return (start - today).days


def head_start_days(opp: Opportunity, today: date) -> int | None:
    """How far ahead of the tender the first public signal came: up to the actual 입찰공고 when
    there is one, else up to the window as shown today (so a window that opened in January
    counts to today). None when the tender itself was the first thing we saw, or when the
    forecast window has passed with no tender — there is no head start to claim then."""
    start, _, passed = shown_window(opp, today)
    if start is None or passed:
        return None
    days = (start - opp.first_seen_at).days
    return days if days > 0 else None


async def institution_names(session: AsyncSession) -> dict[str, str]:
    return dict((await session.execute(select(InstitutionRow.code, InstitutionRow.name))).all())


def card(
    opp: Opportunity,
    names: dict[str, str],
    rec: Recommendation | None,
    today: date,
) -> OpportunityCard:
    window_start, window_end, passed = shown_window(opp, today)
    return OpportunityCard(
        id=opp.id,
        title=opp.title,
        institution=InstitutionRef(
            code=opp.institution_code, name=names.get(opp.institution_code or "", "기관 미상")
        ),
        department=opp.department,
        category=opp.category,
        category_label=category_label(opp.category),
        stage=opp.stage,
        stage_label=STAGE_LABEL[Stage(opp.stage)],
        status=opp.status,
        est_budget_krw=opp.est_budget_krw,
        bid_window_start=window_start,
        bid_window_end=window_end,
        window_passed=passed,
        bid_published_at=opp.bid_published_at,
        tender_out=opp.signal_count > 0 and tender_is_out(opp.stage, opp.bid_published_at),
        conversion_prob=opp.conversion_prob,
        signal_count=opp.signal_count,
        first_seen_at=opp.first_seen_at,
        last_signal_at=opp.last_signal_at,
        score=rec.score if rec else None,
        reasons=explain(opp, rec.breakdown if rec else None),
        feedback=rec.feedback if rec else None,
        lead_days=lead_days(opp, today),
        head_start_days=head_start_days(opp, today),
    )


def signal_out(
    s: Signal,
    doc: Document,
    chunk: DocumentChunk | None,
    link: OpportunitySignal | None,
) -> SignalOut:
    return SignalOut(
        category=s.category,
        id=s.id,
        stage=s.stage,
        stage_label="입찰공고 취소"
        if CANCELS_KEY in s.external_refs
        else STAGE_LABEL[Stage(s.stage)],
        observed_at=s.observed_at,
        title=s.title,
        summary=s.summary,
        department=s.department,
        budget_krw=s.budget_krw,
        expected_year=s.expected_year,
        expected_half=s.expected_half,
        commitment=s.commitment,
        confidence=s.confidence,
        verdict=s.verdict,
        extractor=s.extractor,
        evidence=[
            EvidenceOut(
                quote=str(e.get("quote", "")),
                found=bool(e.get("found")),
                score=float(e.get("score", 0)),
                start=e.get("start"),
                end=e.get("end"),
                method=str(e.get("method", "")),
            )
            for e in s.evidence
        ],
        context=chunk.text if chunk else None,
        context_offset=chunk.char_start if chunk else None,
        document=DocumentRef(
            id=doc.id,
            title=doc.title,
            doc_type=doc.doc_type,
            url=doc.url,
            publisher_raw=doc.publisher_raw,
            parse_method=doc.parse_method,
            published_at=doc.published_at,
        ),
        link=LinkOut(
            score=link.score, method=link.method, tentative=link.tentative, reasons=link.reasons
        )
        if link
        else None,
    )


async def signals_for_opportunity(session: AsyncSession, opp_id: int) -> list[SignalOut]:
    rows = (
        await session.execute(
            select(Signal, Document, DocumentChunk, OpportunitySignal)
            .join(OpportunitySignal, OpportunitySignal.signal_id == Signal.id)
            .join(Document, Document.id == Signal.document_id)
            .outerjoin(DocumentChunk, DocumentChunk.id == Signal.chunk_id)
            .where(OpportunitySignal.opportunity_id == opp_id)
            .order_by(Signal.observed_at, Signal.id)
        )
    ).all()
    return [signal_out(s, d, c, link) for s, d, c, link in rows]
