"""Recommendation: which opportunities should *this* vendor act on, now?

Two-stage retrieval-then-rank, both explainable:

1. **Candidates** — open/bid-open opportunities from (a) pgvector nearest neighbours of the
   company profile embedding and (b) trigram-indexed keyword matches on titles, unioned.
2. **Ranking** — a transparent weighted sum of features. Relevance alone is not enough in B2G:
   a perfectly matching tender that closes in three days is worth less to a sales team than a
   good match whose budget was just approved and whose tender is five months out. Hence the
   ``conversion`` and ``lead_time`` features, which come from the lifecycle stage and the
   backtest-calibrated conversion rates.

Every recommendation stores its feature breakdown, so the UI can say *why* ("예산 편성 완료 ·
키워드 '스마트쉘터' 일치 · 입찰 예상 4~6개월 후") and feedback (relevant / irrelevant / won) can
later train a learning-to-rank model against the same features.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import ColumnElement, delete, or_, select, text, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import today_kst
from app.db.models import CompanyProfile, InstitutionRow, Opportunity, Recommendation
from app.domain.embedding import cosine
from app.domain.institutions import region_matches
from app.domain.text import to_jamo
from app.domain.timing import remaining_window
from app.log import get_logger

log = get_logger(__name__)

RANKER_VERSION = "ranker-v1"
WEIGHTS: dict[str, float] = {
    "semantic": 0.26,
    "keyword": 0.22,
    "category": 0.12,
    "region": 0.08,
    "budget": 0.08,
    "conversion": 0.14,
    "lead_time": 0.10,
}
MAX_PER_ORG = 150
CANDIDATES_PER_SOURCE = 300

# Written as a literal (not bind parameters) so the planner can match the partial index
# ix_opportunities_open_embedding_hnsw even when the driver reuses a generic plan.
OPEN_ONLY = text("opportunities.status IN ('open', 'bid_open')")


@dataclass(slots=True)
class Scored:
    opportunity: Opportunity
    score: float
    breakdown: dict[str, Any]


def _keyword_hits(keywords: list[str], haystack: str) -> list[str]:
    compact = haystack.replace(" ", "")
    jamo = to_jamo(compact)
    hits = []
    for kw in keywords:
        k = kw.replace(" ", "")
        if k and (k in compact or to_jamo(k) in jamo):
            hits.append(kw)
    return hits


def _lead_time_value(opp: Opportunity, today: date) -> tuple[float, int | None]:
    if opp.status == "bid_open":
        return 0.6, 0
    if opp.status in ("closed", "dormant") or opp.bid_window_start is None:
        return 0.0, None
    start, _, passed = remaining_window(opp.bid_window_start, opp.bid_window_end, today)
    if passed:  # the forecast window closed with no tender: late, not imminent
        return 0.35, None
    assert start is not None
    days = (start - today).days
    if days == 0:  # inside the window, tender due any day
        return 0.75, 0
    # Sweet spot: 2–9 months to engage before the RFP is written.
    if days <= 60:
        return 0.7 + 0.3 * days / 60, days
    if days <= 270:
        return 1.0, days
    return max(0.35, 1.0 - (days - 270) / 400), days


def _budget_fit(amount: int | None, lo: int | None, hi: int | None) -> float:
    if amount is None or (lo is None and hi is None):
        return 0.5
    if (lo is None or amount >= lo) and (hi is None or amount <= hi):
        return 1.0
    edge = lo if lo is not None and amount < lo else hi
    assert edge is not None
    return max(0.0, 1.0 - abs(math.log(amount / edge)) / math.log(4))


def score_opportunity(
    opp: Opportunity,
    profile: CompanyProfile,
    region_code: str | None,
    today: date,
) -> Scored:
    haystack = " ".join([opp.title, *opp.keywords])
    hits = _keyword_hits(profile.keywords, haystack)
    excluded = _keyword_hits(profile.exclude_keywords, haystack)
    semantic_raw = (
        cosine(profile.embedding, opp.embedding)
        if profile.embedding is not None and opp.embedding is not None
        else 0.0
    )
    lead, days = _lead_time_value(opp, today)
    region_match = 0.5
    if profile.region_codes and region_code:
        region_match = (
            1.0 if any(region_matches(region_code, r) for r in profile.region_codes) else 0.0
        )
    features = {
        "semantic": min(max((semantic_raw - 0.05) / 0.45, 0.0), 1.0),
        "keyword": min(len(hits) / 2, 1.0),
        "category": (1.0 if opp.category in profile.categories else 0.0)
        if profile.categories
        else 0.5,
        "region": region_match,
        "budget": _budget_fit(opp.est_budget_krw, profile.budget_min, profile.budget_max),
        "conversion": opp.conversion_prob,
        "lead_time": lead,
    }
    score = sum(WEIGHTS[k] * v for k, v in features.items())
    if excluded:
        score *= 0.2
    breakdown: dict[str, Any] = {
        "features": {k: round(v, 3) for k, v in features.items()},
        "weights": WEIGHTS,
        "keyword_hits": hits,
        "excluded_hits": excluded,
        "semantic_raw": round(semantic_raw, 3),
        "days_to_window": days,
        "stage": opp.stage,
    }
    return Scored(opp, round(score, 4), breakdown)


async def _candidates(session: AsyncSession, profile: CompanyProfile) -> list[Opportunity]:
    from app.pipeline.link_state import link_settled

    base = select(Opportunity).where(OPEN_ONLY, link_settled())
    found: dict[int, Opportunity] = {}
    if profile.embedding is not None:
        # An HNSW scan returns at most hnsw.ef_search rows (default 40). Ask for as many as we
        # want back, or "300 nearest" quietly becomes "≤40 nearest" (docs/performance.md).
        await session.execute(text(f"SET LOCAL hnsw.ef_search = {CANDIDATES_PER_SOURCE}"))
        near = await session.scalars(
            base.order_by(Opportunity.embedding.cosine_distance(profile.embedding)).limit(
                CANDIDATES_PER_SOURCE
            )
        )
        found.update({o.id: o for o in near})
    if profile.keywords:
        words = [kw for kw in profile.keywords if kw.strip()]
        conds: list[ColumnElement[bool]] = [Opportunity.title.ilike(f"%{kw}%") for kw in words]
        conds += [Opportunity.keywords.contains([kw]) for kw in words]  # GIN-served `@>`
        if conds:
            kw_rows = await session.scalars(base.where(or_(*conds)).limit(CANDIDATES_PER_SOURCE))
            found.update({o.id: o for o in kw_rows})
    if profile.categories:
        cat_rows = await session.scalars(
            base.where(Opportunity.category.in_(profile.categories)).limit(CANDIDATES_PER_SOURCE)
        )
        found.update({o.id: o for o in cat_rows})
    return list(found.values())


async def refresh_recommendations(
    session: AsyncSession, org_id: int, *, today: date | None = None
) -> list[Scored]:
    today = today or today_kst()
    profile = await session.get(CompanyProfile, org_id)
    if profile is None:
        return []
    candidates = await _candidates(session, profile)
    rows = await session.execute(select(InstitutionRow.code, InstitutionRow.region_code))
    region_by_code: dict[str, str] = {code: region for code, region in rows}  # noqa: C416
    scored = sorted(
        (
            score_opportunity(o, profile, region_by_code.get(o.institution_code or ""), today)
            for o in candidates
        ),
        key=lambda s: s.score,
        reverse=True,
    )[:MAX_PER_ORG]
    now = datetime.now(UTC)
    keep_ids = [s.opportunity.id for s in scored]
    await session.execute(
        delete(Recommendation).where(
            Recommendation.org_id == org_id,
            Recommendation.opportunity_id.not_in(keep_ids) if keep_ids else true(),
            Recommendation.feedback.is_(None),
            Recommendation.feedback_at.is_(None),
            Recommendation.notified_stage.is_(None),
        )
    )
    for s in scored:
        stmt = insert(Recommendation).values(
            org_id=org_id,
            opportunity_id=s.opportunity.id,
            score=s.score,
            breakdown=s.breakdown,
            ranker_version=RANKER_VERSION,
            computed_at=now,
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["org_id", "opportunity_id"],
                set_={
                    "score": s.score,
                    "breakdown": s.breakdown,
                    "ranker_version": RANKER_VERSION,
                    "computed_at": now,
                },
            )
        )
    log.info("recommend.refreshed", org_id=org_id, candidates=len(candidates), kept=len(scored))
    return scored
