"""Opportunity threading — stitch signals about the same purchase across stages and documents.

    council: "냉난방 되는 스마트쉘터 … 3억 5천만원 반영하겠습니다"       (2025-11)
    budget:  "세부사업: 스마트쉘터 설치  352,000"                        (2025-12)
    order:   "2026년 스마트쉘터 제작·설치"          orderPlanUntyNo=R26…  (2026-03)
    prespec: "스마트 버스정류장 조성사업"            orderPlanUntyNo=R26…  (2026-05)
    bid:     "[긴급] 스마트쉘터 구축사업"            bfSpecRgstNo=R26…     (2026-06)

Deterministic where possible, probabilistic where necessary:

1. **Reference links** — 나라장터 records carry each other's numbers (발주계획번호, 사전규격등록번호).
   When present they decide the link (precision 1.0).
2. **Similarity links** — otherwise candidates from the same demand owner are scored on
   semantic similarity, title overlap, category, budget proximity and lifecycle plausibility.
   Above ``link_threshold`` → attach; within ``link_review_band`` of it → attach *tentatively*
   and surface in review; below → start a new opportunity.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import today_kst
from app.db.models import Opportunity, OpportunitySignal, Signal
from app.domain.embedding import cosine
from app.domain.stages import (
    CANCELS_KEY,
    COMMITMENT_MULTIPLIER,
    STAGE_ORDER,
    STAGE_PRIOR,
    Stage,
    forecast_bid_window,
    tender_is_out,
    withdrawn_bids,
)
from app.domain.synonyms import canonical_terms, canonicalize
from app.domain.text import char_ngrams, jaccard
from app.log import get_logger
from app.pipeline.process import canonical_title
from app.runtime import Runtime

log = get_logger(__name__)

WEIGHTS = {"semantic": 0.35, "title": 0.30, "category": 0.15, "budget": 0.10, "timeline": 0.10}
# How far a signal may sit from an opportunity's span, on either side, to be scored against it.
# Symmetric so the order signals are linked in does not decide the answer: a statement linked
# after the budget row it announced has to see that row's opportunity, just as the row, linked
# after the statement, sees the statement's. The one-sided 90 days this replaces left 2026
# 추경 statements (2026-03-12) out of the books' opportunities (2026-06-18) when the books
# went first (docs/real-data-minutes.md §8).
SPAN_DAYS = 720
_COMMITMENT_RANK = {"declined": 0, "reviewing": 1, "planned": 2, "committed": 3}
_REF_KEYS = ("order_plan_no", "prespec_no")


@dataclass(slots=True)
class LinkDecision:
    opportunity_id: int
    score: float
    method: str
    tentative: bool
    reasons: dict[str, Any]


def title_similarity(a: str, b: str) -> float:
    """Character n-gram overlap after synonym canonicalisation, or shared domain terms."""
    ca, cb = canonicalize(canonical_title(a)), canonicalize(canonical_title(b))
    ngram = max(
        jaccard(set(char_ngrams(ca, 2)), set(char_ngrams(cb, 2))),
        jaccard(set(char_ngrams(ca, 3)), set(char_ngrams(cb, 3))),
    )
    ta, tb = canonical_terms(a), canonical_terms(b)
    shared = len(ta & tb) / max(min(len(ta), len(tb)), 1) if ta and tb else 0.0
    return max(ngram, 0.8 * shared)


def budget_similarity(a: int | None, b: int | None) -> float:
    if not a or not b:
        return 0.5  # unknown: neither evidence for nor against
    ratio = abs(math.log(a / b))
    return max(0.0, 1.0 - ratio / math.log(3))


def timeline_plausibility(signal: Signal, opp: Opportunity) -> float:
    s_rank = STAGE_ORDER[Stage(signal.stage)]
    o_rank = STAGE_ORDER[Stage(opp.stage)]
    # Days between the signal and the opportunity's span, before its first signal or after
    # its last — the same distance whichever of the two was linked first.
    if signal.observed_at > opp.last_signal_at:
        gap_days = (signal.observed_at - opp.last_signal_at).days
    elif signal.observed_at < opp.first_seen_at:
        gap_days = (opp.first_seen_at - signal.observed_at).days
    else:
        gap_days = 0
    score = 1.0
    if gap_days > 540:
        score -= 0.6  # a year and a half of silence: probably a different project
    if (
        s_rank < o_rank
        and opp.bid_published_at
        and signal.observed_at > opp.bid_published_at + timedelta(days=60)
    ):
        score -= 0.7  # talk about a project whose tender already happened → next phase
    return max(score, 0.0)


def terms_conflict(a: str, b: str) -> bool:
    """Both titles name specific, *different* things ("스마트쉘터" vs "스마트폴")."""
    ta, tb = canonical_terms(a), canonical_terms(b)
    return bool(ta) and bool(tb) and not (ta & tb)


def score_candidate(signal: Signal, opp: Opportunity) -> tuple[float, dict[str, float]]:
    parts = {
        "semantic": max(cosine(signal.embedding, opp.embedding), 0.0)
        if signal.embedding is not None and opp.embedding is not None
        else 0.0,
        "title": title_similarity(signal.title, opp.title),
        "category": 1.0
        if signal.category == opp.category
        else (0.5 if "other" in (signal.category, opp.category) else 0.0),
        "budget": budget_similarity(signal.budget_krw, opp.est_budget_krw),
        "timeline": timeline_plausibility(signal, opp),
    }
    total = sum(WEIGHTS[k] * v for k, v in parts.items())
    if terms_conflict(signal.title, opp.title):
        total -= 0.15
        parts["conflict"] = 1.0
    return round(total, 4), {k: round(v, 3) for k, v in parts.items()}


async def _reference_match(session: AsyncSession, signal: Signal) -> int | None:
    for key in _REF_KEYS:
        value = signal.external_refs.get(key)
        if not value:
            continue
        opp_id = await session.scalar(
            select(OpportunitySignal.opportunity_id)
            .join(Signal, Signal.id == OpportunitySignal.signal_id)
            .where(Signal.id != signal.id, Signal.external_refs.contains({key: value}))
            .limit(1)
        )
        if opp_id is not None:
            return int(opp_id)
    bid_no = signal.external_refs.get("bid_notice_no")
    if bid_no:
        # Another 차수 of the same 공고 (변경·재공고·취소) is the same purchase.
        opp_id = await session.scalar(
            select(OpportunitySignal.opportunity_id)
            .join(Signal, Signal.id == OpportunitySignal.signal_id)
            .where(Signal.id != signal.id, Signal.external_refs.contains({"bid_notice_no": bid_no}))
            .limit(1)
        )
        if opp_id is not None:
            return int(opp_id)
        # A 사전규격 lists the bid numbers it turned into.
        opp_id = await session.scalar(
            select(OpportunitySignal.opportunity_id)
            .join(Signal, Signal.id == OpportunitySignal.signal_id)
            .where(Signal.external_refs.contains({"bid_notice_nos": [bid_no]}))
            .limit(1)
        )
        if opp_id is not None:
            return int(opp_id)
    # …and so do 발주계획 (72% of live plans, 2026-09-26). Half of them were registered the
    # same day as their 공고, so the bid can be linked first; look the other way too.
    for no in signal.external_refs.get("bid_notice_nos") or ():
        opp_id = await session.scalar(
            select(OpportunitySignal.opportunity_id)
            .join(Signal, Signal.id == OpportunitySignal.signal_id)
            .where(Signal.id != signal.id, Signal.external_refs.contains({"bid_notice_no": no}))
            .limit(1)
        )
        if opp_id is not None:
            return int(opp_id)
    return None


async def _candidates(session: AsyncSession, signal: Signal, limit: int = 12) -> list[Opportunity]:
    stmt = select(Opportunity).where(
        Opportunity.institution_code == signal.institution_code,
        Opportunity.last_signal_at >= signal.observed_at - timedelta(days=SPAN_DAYS),
        Opportunity.first_seen_at <= signal.observed_at + timedelta(days=SPAN_DAYS),
    )
    if signal.embedding is not None:
        stmt = stmt.order_by(Opportunity.embedding.cosine_distance(signal.embedding))
    return list((await session.scalars(stmt.limit(limit))).all())


async def _without_conflicting_numbers(
    session: AsyncSession, signal: Signal, candidates: list[Opportunity]
) -> list[Opportunity]:
    """Drop opportunities that already hold a *different* 발주계획번호 or 사전규격번호 than the
    signal: two numbers are two purchases, however alike the titles. On 30 days of live data
    (2026-09-26) 1,396 of 1,987 similarity links joined such pairs — one 기관 puts out dozens
    of "…도로 정비공사" a month."""
    mine = {k: signal.external_refs[k] for k in _REF_KEYS if signal.external_refs.get(k)}
    if not mine or not candidates:
        return candidates
    rows = await session.execute(
        select(OpportunitySignal.opportunity_id, Signal.external_refs)
        .join(Signal, Signal.id == OpportunitySignal.signal_id)
        .where(OpportunitySignal.opportunity_id.in_([o.id for o in candidates]))
    )
    conflicting = {
        opp_id
        for opp_id, refs in rows
        if any(refs.get(k) and refs[k] != v for k, v in mine.items())
    }
    return [o for o in candidates if o.id not in conflicting]


async def _without_rows_of_the_same_book(
    session: AsyncSession, signal: Signal, candidates: list[Opportunity]
) -> list[Opportunity]:
    """Drop opportunities that already hold another row of the same 예산서: a book lists each
    세부사업 once, so two rows are two projects. On six live 성남시 books (2026-09-27), 1,542 of
    2,327 rows ended up in opportunities holding more than one 사업명 — up to 32 in one, and one
    held 54 signals around "…정비공사" rows."""
    if signal.stage != "budget_line" or not candidates:
        return candidates
    rows = await session.scalars(
        select(OpportunitySignal.opportunity_id)
        .join(Signal, Signal.id == OpportunitySignal.signal_id)
        .where(
            OpportunitySignal.opportunity_id.in_([o.id for o in candidates]),
            Signal.document_id == signal.document_id,
            Signal.id != signal.id,
        )
    )
    taken = set(rows.all())
    return [o for o in candidates if o.id not in taken]


async def decide(session: AsyncSession, runtime: Runtime, signal: Signal) -> LinkDecision | None:
    ref = await _reference_match(session, signal)
    if ref is not None:
        return LinkDecision(ref, 1.0, "ref", False, {"ref": signal.external_refs})
    if signal.institution_code is None:
        return None
    threshold = runtime.settings.link_threshold
    band = runtime.settings.link_review_band
    candidates = await _without_rows_of_the_same_book(
        session,
        signal,
        await _without_conflicting_numbers(session, signal, await _candidates(session, signal)),
    )
    best: tuple[float, Opportunity, dict[str, float]] | None = None
    for opp in candidates:
        score, parts = score_candidate(signal, opp)
        if best is None or score > best[0]:
            best = (score, opp, parts)
    if best is None:
        return None
    score, opp, parts = best
    # Exclusivity: the only live opportunity at this institution in the same category with a
    # budget within ~15% is strong evidence even when the names share nothing ("어린이보호구역
    # 지능형 CCTV" → "스쿨존 AI 안전카메라"). Never applies when two candidates compete.
    same_kind = [
        o
        for o in candidates
        if o.category == signal.category
        and budget_similarity(signal.budget_krw, o.est_budget_krw) >= 0.85
        and timeline_plausibility(signal, o) >= 0.9
    ]
    if (
        len(same_kind) == 1
        and same_kind[0].id == opp.id
        and signal.budget_krw
        and opp.est_budget_krw
        and not terms_conflict(signal.title, opp.title)
    ):
        score = round(score + 0.2, 4)
        parts = parts | {"exclusive": 1.0}
    if score < threshold - band:
        return None
    return LinkDecision(opp.id, score, "similarity", score < threshold, parts)


def _conversion_probability(
    stage: Stage,
    commitment: str | None,
    corroboration: int,
    calibration: dict[str, float] | None = None,
) -> float:
    if tender_is_out(stage, None):
        return 1.0
    key = f"{stage.value}:{commitment or 'none'}"
    if calibration and key in calibration:
        base = calibration[key]
    else:
        base = STAGE_PRIOR[stage]
        if stage is Stage.COUNCIL and commitment:
            base *= COMMITMENT_MULTIPLIER.get(commitment, 1.0)
    return round(min(0.98, base + 0.05 * min(max(corroboration - 1, 0), 3)), 3)


async def refresh_opportunity(
    session: AsyncSession,
    opp: Opportunity,
    *,
    today: date,
    calibration: dict[str, float] | None = None,
) -> None:
    links = list(
        (
            await session.scalars(
                select(Signal)
                .join(OpportunitySignal, OpportunitySignal.signal_id == Signal.id)
                .where(OpportunitySignal.opportunity_id == opp.id)
                .order_by(Signal.observed_at)
            )
        ).all()
    )
    if not links:
        await session.delete(opp)
        return
    withdrawn = withdrawn_bids(
        (s.external_refs, s.observed_at) for s in links if s.stage == Stage.BID.value
    )
    # A 취소공고, and a 공고 it withdrew, say no tender is out: the stage, the window and the
    # status come from what is left.
    live = [
        s
        for s in links
        if not (
            s.stage == Stage.BID.value
            and (
                CANCELS_KEY in s.external_refs or s.external_refs.get("bid_notice_no") in withdrawn
            )
        )
    ]
    by_stage = sorted(live or links, key=lambda s: (STAGE_ORDER[Stage(s.stage)], s.observed_at))
    top = by_stage[-1]
    stage = Stage(top.stage)
    opp.stage = stage.value
    opp.first_seen_at = min(s.observed_at for s in links)
    opp.last_signal_at = max(s.observed_at for s in links)
    opp.signal_count = len(links)
    formal = [s for s in by_stage if STAGE_ORDER[Stage(s.stage)] >= STAGE_ORDER[Stage.BUDGET]]
    opp.title = canonical_title((formal[-1] if formal else top).title)
    cats = Counter(s.category for s in links if s.category != "other")
    opp.category = cats.most_common(1)[0][0] if cats else top.category
    depts = Counter(s.department for s in links if s.department)
    opp.department = depts.most_common(1)[0][0] if depts else None
    budgets = [s.budget_krw for s in by_stage if s.budget_krw]
    opp.est_budget_krw = budgets[-1] if budgets else None
    commitments = [s.commitment for s in links if s.commitment]
    opp.best_commitment = (
        max(commitments, key=lambda c: _COMMITMENT_RANK.get(c, 0)) if commitments else None
    )
    kw: list[str] = []
    for s in reversed(by_stage):
        for k in s.keywords:
            if k not in kw:
                kw.append(k)
    opp.keywords = kw[:10]
    vectors = [s.embedding for s in links if s.embedding is not None]
    if vectors:
        dim = len(vectors[0])
        mean = [sum(float(v[i]) for v in vectors) / len(vectors) for i in range(dim)]
        norm = math.sqrt(sum(x * x for x in mean)) or 1.0
        opp.embedding = [x / norm for x in mean]
    bids = [s for s in live if s.stage in (Stage.BID.value, Stage.AWARD.value)]
    opp.bid_published_at = min(s.observed_at for s in bids) if bids else None
    if opp.bid_published_at:
        opp.bid_window_start = opp.bid_window_end = opp.bid_published_at
        opp.status = "bid_open" if (today - opp.bid_published_at).days <= 21 else "closed"
    elif not live:
        # Only a 공고 and its 취소, nothing earlier to say the project lives on: that tender is
        # over. A 재공고 carrying the same 발주계획 or 공고 number links here and reopens it.
        opp.bid_published_at = min(s.observed_at for s in links)
        opp.bid_window_start = opp.bid_window_end = opp.bid_published_at
        opp.status = "closed"
    else:
        timed = [s for s in by_stage if s.expected_year]
        basis = timed[-1] if timed else top
        start, end = forecast_bid_window(
            Stage(basis.stage),
            basis.observed_at,
            expected_year=basis.expected_year,
            expected_half=basis.expected_half,  # type: ignore[arg-type]
        )
        opp.bid_window_start, opp.bid_window_end = start, end
        dormant = (today - opp.last_signal_at).days > 540 or end < today - timedelta(days=180)
        opp.status = "dormant" if dormant else "open"
    documents = {s.document_id for s in links}
    opp.conversion_prob = _conversion_probability(
        stage, opp.best_commitment, len(documents), calibration
    )


async def link_signals(
    session: AsyncSession,
    runtime: Runtime,
    signal_ids: list[int],
    *,
    today: date | None = None,
    calibration: dict[str, float] | None = None,
) -> list[int]:
    """Link accepted, not-yet-linked signals. Returns ids of touched opportunities."""
    today = today or today_kst()
    signals = list(
        (
            await session.scalars(
                select(Signal)
                .outerjoin(OpportunitySignal, OpportunitySignal.signal_id == Signal.id)
                .where(
                    Signal.id.in_(signal_ids),
                    Signal.verdict == "accepted",
                    OpportunitySignal.signal_id.is_(None),
                )
                .order_by(Signal.observed_at, Signal.id)
            )
        ).all()
    )
    touched: set[int] = set()
    for signal in signals:
        decision = await decide(session, runtime, signal)
        if decision is None:
            opp = Opportunity(
                institution_code=signal.institution_code,
                department=signal.department,
                title=canonical_title(signal.title),
                category=signal.category,
                stage=signal.stage,
                status="open",
                first_seen_at=signal.observed_at,
                last_signal_at=signal.observed_at,
                est_budget_krw=signal.budget_krw,
                embedding=signal.embedding,
                keywords=signal.keywords,
            )
            session.add(opp)
            await session.flush()
            session.add(
                OpportunitySignal(
                    opportunity_id=opp.id,
                    signal_id=signal.id,
                    score=1.0,
                    method="seed",
                    tentative=False,
                    reasons={},
                )
            )
        else:
            opp_or_none = await session.get(Opportunity, decision.opportunity_id)
            assert opp_or_none is not None
            opp = opp_or_none
            session.add(
                OpportunitySignal(
                    opportunity_id=opp.id,
                    signal_id=signal.id,
                    score=decision.score,
                    method=decision.method,
                    tentative=decision.tentative,
                    reasons=decision.reasons,
                )
            )
        await session.flush()
        await refresh_opportunity(session, opp, today=today, calibration=calibration)
        await session.flush()
        touched.add(opp.id)
    log.info("link.done", signals=len(signals), opportunities=len(touched))
    return sorted(touched)
