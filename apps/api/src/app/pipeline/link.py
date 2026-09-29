"""Opportunity threading — stitch signals about the same purchase across stages and documents.

    council: "냉난방 되는 스마트쉘터 … 3억 5천만원 반영하겠습니다"       (2025-11)
    budget:  "세부사업: 스마트쉘터 설치  352,000"                        (2025-12)
    order:   "2026년 스마트쉘터 제작·설치"          orderPlanUntyNo=R26…  (2026-03)
    prespec: "스마트 버스정류장 조성사업"            orderPlanUntyNo=R26…  (2026-05)
    bid:     "[긴급] 스마트쉘터 구축사업"            bfSpecRgstNo=R26…     (2026-06)

Deterministic where possible, probabilistic where necessary:

1. **Reference links** — 나라장터 records carry each other's numbers (발주계획번호, 사전규격등록번호).
   They decide the link only within one institution and one consistent purchase identity.
2. **Similarity links** — otherwise candidates from the same demand owner are scored on
   semantic similarity, title overlap, category, budget proximity and lifecycle plausibility.
   Above ``link_threshold`` with a clear lead over alternatives → attach; otherwise keep a
   separate opportunity. An ambiguous match must not change another opportunity's summary.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.sql.elements import ColumnElement

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
# A 세부사업 keeps its name from one book to the next, give or take spacing or a word added
# ("시설개선" → "시설 개선 공사"). Below this similarity, or with a word replaced, two rows are two
# projects however alike their field and amount (docs/real-data-minutes.md §10).
BUDGET_NAME_FLOOR = 0.5
_NAME_NOISE_RE = re.compile(r"[\s()\[\]{}·ㆍ,.\-_/]")
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


def budget_names_agree(a: str, b: str) -> bool:
    """Two 세부사업명 from different books name the same project: one is the other with words
    added ("여수동 (공공부지) 복합문화시설 조성"). A word replaced — "중원청소년수련관" for
    "수정청소년수련관", 1 for 2 — or words dropped on one side and others added on the other —
    "CCTV 관제센터 구축 및 운영" and "수정구 생활안전 CCTV 구축" — make another project, however
    much of the rest is shared."""
    x, y = (_NAME_NOISE_RE.sub("", canonicalize(canonical_title(t))) for t in (a, b))
    if x == y:
        return True
    if title_similarity(a, b) < BUDGET_NAME_FLOOR:
        return False
    edits = {tag for tag, *_ in SequenceMatcher(None, x, y, autojunk=False).get_opcodes()}
    return edits in ({"equal", "insert"}, {"equal", "delete"})


def terms_conflict(a: str, b: str) -> bool:
    """Both titles name specific, *different* things ("스마트쉘터" vs "스마트폴")."""
    ta, tb = canonical_terms(a), canonical_terms(b)
    return bool(ta) and bool(tb) and not (ta & tb)


_FACILITY_RE = re.compile(r"([가-힣0-9]+?)(청소년수련관|도서관)")
_GENERIC_FACILITY_PREFIXES = {"공공", "시립", "구립", "국립", "작은", "어린이", "스마트", "전자"}


def facilities_conflict(a: str, b: str) -> bool:
    """A shared facility type is not identity when both titles name different sites.

    Intentionally narrow: no claim to resolve all Korean place names. Generic descriptions
    such as 공공도서관 carry no location evidence and therefore do not trigger this veto.
    """

    def names(title: str) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for prefix, kind in _FACILITY_RE.findall(title):
            if prefix not in _GENERIC_FACILITY_PREFIXES:
                result.setdefault(kind, set()).add(prefix)
        return result

    left, right = names(a), names(b)
    return any(
        not any(x.endswith(y) or y.endswith(x) for x in left[kind] for y in right[kind])
        for kind in left.keys() & right.keys()
    )


def score_candidate(signal: Signal, opp: Opportunity) -> tuple[float, dict[str, float]]:
    if facilities_conflict(signal.title, opp.title):
        return 0.0, {"facility_conflict": 1.0}
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


class _ReferenceConflictError(Exception):
    """Reference evidence is contradictory; similarity must not override it."""


def _bid_numbers(refs: dict[str, Any]) -> set[str]:
    numbers = {str(no) for no in refs.get("bid_notice_nos") or () if no}
    for key in ("bid_notice_no", CANCELS_KEY):
        if no := refs.get(key):
            numbers.add(str(no))
    return numbers


async def _reference_match(session: AsyncSession, signal: Signal) -> int | None:
    if signal.institution_code is None:
        return None
    # One plan/specification can advertise several tenders. It cannot identify one purchase.
    bid_numbers = _bid_numbers(signal.external_refs)
    if len(bid_numbers) > 1:
        raise _ReferenceConflictError
    conditions: list[ColumnElement[bool]] = []
    for key in _REF_KEYS:
        value = signal.external_refs.get(key)
        if value:
            conditions.append(Signal.external_refs.contains({key: value}))
    bid_conditions: list[ColumnElement[bool]] = []
    for no in bid_numbers:
        # Same-number revisions/cancellations retain identity, in either arrival order.
        bid_conditions.extend(
            Signal.external_refs.contains({key: value})
            for key, value in (
                ("bid_notice_no", no),
                (CANCELS_KEY, no),
                ("bid_notice_nos", [no]),
            )
        )
    conditions.extend(bid_conditions)
    if not conditions:
        return None
    stmt = (
        select(Opportunity)
        .join(OpportunitySignal, OpportunitySignal.opportunity_id == Opportunity.id)
        .join(Signal, Signal.id == OpportunitySignal.signal_id)
        .where(
            Signal.id != signal.id,
            Signal.institution_code == signal.institution_code,
            Opportunity.institution_code == signal.institution_code,
            Signal.verdict == "accepted",
            OpportunitySignal.tentative.is_(False),
        )
        .distinct()
        .order_by(Opportunity.id)
    )
    matches = list((await session.scalars(stmt.where(or_(*conditions)))).all())
    if not matches:
        return None
    if len(matches) > 1 and bid_conditions:
        # A shared plan can now hold separate tenders. A cancellation/revision must still
        # find its own bid, rather than be defeated by the other tender's upstream number.
        direct = list((await session.scalars(stmt.where(or_(*bid_conditions)))).all())
        if direct:
            matches = direct
    # Inspect ALL reference kinds/targets. The first row is not necessarily the right one,
    # and filtering one conflicting target must not make another target look unambiguous.
    if len(matches) != 1 or not await _without_conflicting_numbers(session, signal, matches):
        raise _ReferenceConflictError
    return matches[0].id


async def _candidates(session: AsyncSession, signal: Signal) -> list[Opportunity]:
    eligible_member = (
        select(OpportunitySignal.signal_id)
        .join(Signal, Signal.id == OpportunitySignal.signal_id)
        .where(
            OpportunitySignal.opportunity_id == Opportunity.id,
            OpportunitySignal.tentative.is_(False),
            Signal.verdict == "accepted",
            Signal.institution_code == signal.institution_code,
        )
        .exists()
    )
    stmt = (
        select(Opportunity)
        .options(defer(Opportunity.embedding, raiseload=True))
        .where(
            Opportunity.institution_code == signal.institution_code,
            Opportunity.signal_count > 0,
            Opportunity.last_signal_at >= signal.observed_at - timedelta(days=SPAN_DAYS),
            Opportunity.first_seen_at <= signal.observed_at + timedelta(days=SPAN_DAYS),
            eligible_member,
        )
    )
    # Score the complete eligible population: filtering a nearest-12 slice hid valid #13,
    # and vector order cannot bound the combined title/category/budget/timeline score.
    # ID ordering also makes ties deterministic, including signals without embeddings.
    return list((await session.scalars(stmt.order_by(Opportunity.id))).all())


async def _load_candidate_embeddings(session: AsyncSession, candidates: list[Opportunity]) -> None:
    """Fetch vectors only after every structural gate, in one explicit query.

    Mark them loaded without marking the opportunities dirty. Deferred attributes raise on
    access, so scoring can never accidentally issue one lazy query per candidate.
    """
    rows = await session.execute(
        select(Opportunity.id, Opportunity.embedding).where(
            Opportunity.id.in_([opp.id for opp in candidates])
        )
    )
    vectors = dict(rows.all())
    for opp in candidates:
        set_committed_value(opp, "embedding", vectors.get(opp.id))


async def _without_conflicting_numbers(
    session: AsyncSession, signal: Signal, candidates: list[Opportunity]
) -> list[Opportunity]:
    """Keep one consistent procurement identity, including all members' bid numbers.

    A shared plan/specification is insufficient to merge distinct tenders. Plural bid lists
    and already-mixed threads are not safe identities for a single purchase either.
    """
    mine = {k: signal.external_refs[k] for k in _REF_KEYS if signal.external_refs.get(k)}
    mine_bids = _bid_numbers(signal.external_refs)
    if len(mine_bids) > 1:
        return []
    if not candidates:
        return candidates
    rows = await session.execute(
        select(OpportunitySignal.opportunity_id, Signal.external_refs)
        .join(Signal, Signal.id == OpportunitySignal.signal_id)
        .where(
            OpportunitySignal.opportunity_id.in_([o.id for o in candidates]),
            Signal.verdict == "accepted",
            OpportunitySignal.tentative.is_(False),
        )
    )
    conflicting = set()
    bids: dict[int, set[str]] = {}
    for opp_id, refs in rows:
        bids.setdefault(opp_id, set(mine_bids)).update(_bid_numbers(refs))
        if len(bids[opp_id]) > 1 or any(refs.get(k) and refs[k] != v for k, v in mine.items()):
            conflicting.add(opp_id)
    return [o for o in candidates if o.id not in conflicting]


async def _without_other_budget_rows(
    session: AsyncSession, signal: Signal, candidates: list[Opportunity]
) -> list[Opportunity]:
    """For a 예산서 row, drop opportunities that already hold another row of the same book, or a
    row of another book under another name (``budget_names_agree``).

    A book lists each 세부사업 once, so two of its rows are two projects: on six live 성남시 books
    (2026-09-27), 1,542 of 2,327 rows ended up in opportunities holding more than one 사업명 — up
    to 32 in one. Across books the name is what carries a project from year to year; field,
    amount and wording alike put "중원청소년수련관 시설개선" (2025) and "수정청소년수련관 시설 개선"
    (2026 추경) in one opportunity (docs/real-data-minutes.md §9.4).

    One name can stand for many projects, though: every 동 of a city carries its own "소규모
    정비공사" or "전국동시지방선거 추진" (up to 50 rows of one name in one 성남시 book). When a
    candidate holds a same-name row of this row's department, candidates holding it only under
    other departments are dropped too; and when the name is generic — this row's own book, or
    another, has it in more than one opportunity — only this row's department joins. Otherwise the
    department decides nothing: 26 of 454 names that are unique in their books changed
    department between books (§10), 여수동 복합문화시설 among them."""
    if signal.stage != "budget_line" or not candidates:
        return candidates
    rows = (
        await session.execute(
            select(
                OpportunitySignal.opportunity_id,
                Signal.document_id,
                Signal.title,
                Signal.department,
            )
            .join(Signal, Signal.id == OpportunitySignal.signal_id)
            .where(
                OpportunitySignal.opportunity_id.in_([o.id for o in candidates]),
                Signal.stage == signal.stage,
                Signal.id != signal.id,
                Signal.verdict == "accepted",
                OpportunitySignal.tentative.is_(False),
            )
        )
    ).all()
    agree = [budget_names_agree(signal.title, title) for _, _, title, _ in rows]
    apart = {
        opp_id
        for (opp_id, document_id, _, _), same in zip(rows, agree, strict=True)
        if document_id == signal.document_id or not same
    }
    named = {opp_id for opp_id, *_ in rows if opp_id not in apart}
    same_department = {
        opp_id for opp_id, _, _, dept in rows if opp_id in named and dept == signal.department
    }
    # Generic: some book, this one included, has the name in more than one candidate. Its own
    # book counts too — once earlier rows of this book have taken their opportunities out (the
    # same-book rule), the last 동 would otherwise see a single candidate left and take it.
    holders: dict[int, set[int]] = {}
    for (opp_id, document_id, _, _), same in zip(rows, agree, strict=True):
        if same:
            holders.setdefault(document_id, set()).add(opp_id)
    generic = signal.document_id in holders or any(len(o) > 1 for o in holders.values())
    if signal.department and (same_department or generic):
        apart |= named - same_department
    return [o for o in candidates if o.id not in apart]


async def decide(session: AsyncSession, runtime: Runtime, signal: Signal) -> LinkDecision | None:
    try:
        ref = await _reference_match(session, signal)
    except _ReferenceConflictError:
        return None
    if ref is not None:
        return LinkDecision(ref, 1.0, "ref", False, {"ref": signal.external_refs})
    if signal.institution_code is None:
        return None
    threshold = runtime.settings.link_threshold
    band = runtime.settings.link_review_band
    candidates = await _without_other_budget_rows(
        session,
        signal,
        await _without_conflicting_numbers(session, signal, await _candidates(session, signal)),
    )
    if candidates and signal.embedding is not None:
        await _load_candidate_embeddings(session, candidates)
    best: tuple[float, Opportunity, dict[str, float]] | None = None
    for opp in candidates:
        score, parts = score_candidate(signal, opp)
        if best is None or score > best[0]:
            best = (score, opp, parts)
    if best is None:
        return None
    score, opp, parts = best
    # Similar amount/category can reinforce positive title evidence, never replace it.
    # Uniqueness is checked over every eligible candidate in the institution/time window.
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
        and not facilities_conflict(signal.title, opp.title)
        and parts.get("title", 0.0) >= BUDGET_NAME_FLOOR
    ):
        score = round(score + 0.2, 4)
        parts = parts | {"exclusive": 1.0}
    runner_up = max(
        (score_candidate(signal, o)[0] for o in candidates if o.id != opp.id), default=0.0
    )
    if score < threshold or score - runner_up < band:
        return None
    return LinkDecision(opp.id, score, "similarity", False, parts)


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
                .where(
                    OpportunitySignal.opportunity_id == opp.id,
                    Signal.verdict == "accepted",
                    OpportunitySignal.tentative.is_(False),
                )
                .order_by(Signal.observed_at, Signal.id)
            )
        ).all()
    )
    if not links:
        # Keep the identity and its feedback/brief/review history, but retract all derived
        # demand claims when no accepted, confirmed evidence remains.
        opp.status = "dormant"
        opp.signal_count = 0
        opp.bid_published_at = None
        opp.bid_window_start = opp.bid_window_end = None
        opp.est_budget_krw = None
        opp.best_commitment = None
        opp.conversion_prob = 0.0
        opp.keywords = []
        opp.embedding = None
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
    else:
        opp.embedding = None
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
