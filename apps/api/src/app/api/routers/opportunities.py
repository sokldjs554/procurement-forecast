from __future__ import annotations

import base64
import json
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, status
from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.orm import InstrumentedAttribute

from app.api.deps import PrincipalDep, RuntimeDep, SessionDep
from app.api.presenters import card, institution_names, signals_for_opportunity
from app.api.schemas import (
    BriefOut,
    BudgetPoint,
    FeedbackIn,
    FeedPage,
    OpportunityDetail,
)
from app.billing.ledger import InsufficientCreditsError
from app.clock import today_kst
from app.db.models import Brief, Opportunity, Recommendation
from app.domain.stages import STAGE_LABEL, Stage
from app.pipeline.brief import OpportunityNotFoundError, generate_brief

router = APIRouter(prefix="/api/opportunities", tags=["opportunities"])


FeedSort = Literal["score", "soon", "recent"]


def _sort_key(
    sort: FeedSort, today: date
) -> tuple[ColumnElement[Any] | InstrumentedAttribute[Any], bool]:
    """(SQL key, ascending). "soon" orders by when the tender is due: a forecast window that
    has already opened, or a tender already out, counts as today; no forecast sorts last."""
    if sort == "soon":
        return func.greatest(func.coalesce(Opportunity.bid_window_start, date.max), today), True
    if sort == "recent":
        return Opportunity.last_signal_at, False
    return Recommendation.score, False


def _encode_cursor(sort: FeedSort, value: Any, score: float, opp_id: int, day: date) -> str:
    raw = value.isoformat() if isinstance(value, date) else value
    payload = [sort, raw, score, opp_id, day.isoformat()]
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _decode_cursor(cursor: str, sort: FeedSort) -> tuple[Any, float, int, date | None]:
    """(key, score, id, day the first page was served). The day pins "soon", whose key depends
    on today, so a page fetched after midnight continues the same ordering."""
    try:
        parts = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if len(parts) == 2 and sort == "score":  # issued before there were sort options
            score, opp_id = parts
            return float(score), float(score), int(opp_id), None
        cursor_sort, raw, score, opp_id, day = parts
        if cursor_sort != sort:
            raise ValueError("cursor from another sort order")
        value = float(raw) if sort == "score" else date.fromisoformat(raw)
        return value, float(score), int(opp_id), date.fromisoformat(day)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid cursor") from exc


@router.get("", response_model=FeedPage)
async def feed(
    principal: PrincipalDep,
    session: SessionDep,
    stage: Annotated[list[str] | None, Query()] = None,
    category: Annotated[list[str] | None, Query()] = None,
    status_: Annotated[list[str] | None, Query(alias="status")] = None,
    q: str | None = None,
    include_dismissed: bool = False,
    sort: FeedSort = "score",
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> FeedPage:
    """The recommended feed, keyset-paginated on (sort key, id desc). ``stage_counts`` counts
    the same filter per stage, ignoring the stage filter itself, for the stage chips."""
    conds = [Recommendation.org_id == principal.org.id]
    if category:
        conds.append(Opportunity.category.in_(category))
    conds.append(Opportunity.status.in_(status_ or ["open", "bid_open"]))
    if q:
        conds.append(or_(Opportunity.title.ilike(f"%{q}%"), Opportunity.keywords.contains([q])))
    if not include_dismissed:
        conds.append(
            or_(
                Recommendation.feedback.is_(None),
                Recommendation.feedback.not_in(("dismissed", "irrelevant")),
            )
        )
    joined = select(Recommendation, Opportunity).join(
        Opportunity, Opportunity.id == Recommendation.opportunity_id
    )
    per_stage = await session.execute(
        select(Opportunity.stage, func.count())
        .select_from(Recommendation)
        .join(Opportunity, Opportunity.id == Recommendation.opportunity_id)
        .where(and_(*conds))
        .group_by(Opportunity.stage)
    )
    stage_counts: dict[str, int] = {row[0]: row[1] for row in per_stage}
    if stage:
        conds.append(Opportunity.stage.in_(stage))
    base = joined.where(and_(*conds))
    total = (
        sum(n for st, n in stage_counts.items() if st in stage)
        if stage
        else sum(stage_counts.values())
    )
    today = today_kst()
    c_value = c_score = c_id = None
    day = today
    if cursor:
        c_value, c_score, c_id, c_day = _decode_cursor(cursor, sort)
        day = c_day or today
    key, ascending = _sort_key(sort, day)
    # Ties on the sort key (every window already open counts as "today") go to the better fit.
    order = [
        key.asc() if ascending else key.desc(),
        Recommendation.score.desc(),
        Opportunity.id.desc(),
    ]
    page_q = base.add_columns(key.label("sort_key"))
    if cursor:
        beyond = key > c_value if ascending else key < c_value
        after_tie = or_(
            Recommendation.score < c_score,
            and_(Recommendation.score == c_score, Opportunity.id < c_id),
        )
        page_q = page_q.where(or_(beyond, and_(key == c_value, after_tie)))
    rows = (await session.execute(page_q.order_by(*order).limit(limit + 1))).all()
    names = await institution_names(session)
    items = [card(opp, names, rec, today) for rec, opp, _ in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        last_rec, last_opp, last_key = rows[limit - 1]
        next_cursor = _encode_cursor(sort, last_key, last_rec.score, last_opp.id, day)
    return FeedPage(items=items, next_cursor=next_cursor, total=total, stage_counts=stage_counts)


@router.get("/{opportunity_id}", response_model=OpportunityDetail)
async def detail(
    opportunity_id: int, principal: PrincipalDep, session: SessionDep
) -> OpportunityDetail:
    opp = await session.get(Opportunity, opportunity_id)
    if opp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "이 사업을 찾을 수 없어요")
    rec = await session.get(Recommendation, (principal.org.id, opp.id))
    names = await institution_names(session)
    base = card(opp, names, rec, today_kst())
    signals = await signals_for_opportunity(session, opp.id)
    trajectory = [
        BudgetPoint(
            observed_at=s.observed_at,
            amount=s.budget_krw,
            stage=s.stage,
            stage_label=STAGE_LABEL[Stage(s.stage)],
        )
        for s in signals
        if s.budget_krw
    ]
    briefs = (
        await session.scalars(
            select(Brief)
            .where(Brief.org_id == principal.org.id, Brief.opportunity_id == opp.id)
            .order_by(Brief.created_at.desc())
        )
    ).all()
    return OpportunityDetail(
        **base.model_dump(),
        keywords=opp.keywords,
        best_commitment=opp.best_commitment,
        signals=signals,
        budget_trajectory=trajectory,
        breakdown=rec.breakdown if rec else None,
        briefs=[BriefOut.model_validate(b) for b in briefs],
    )


@router.post("/{opportunity_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def feedback(
    opportunity_id: int, body: FeedbackIn, principal: PrincipalDep, session: SessionDep
) -> None:
    # Publish customer anchors only while holding the automatic link writer's identity lock.
    await session.get(Opportunity, opportunity_id, with_for_update=True, populate_existing=True)
    rec = await session.get(
        Recommendation, (principal.org.id, opportunity_id), populate_existing=True
    )
    if rec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "이 사업은 아직 추천 목록에 없어요")
    if body.feedback is not None:
        from app.pipeline.link_state import capture_customer_anchor

        await capture_customer_anchor(session, opportunity_id)
    rec.feedback = body.feedback
    rec.feedback_at = datetime.now(UTC) if body.feedback else None


@router.post(
    "/{opportunity_id}/briefs", response_model=BriefOut, status_code=status.HTTP_201_CREATED
)
async def create_brief(
    opportunity_id: int,
    principal: PrincipalDep,
    session: SessionDep,
    runtime: RuntimeDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=100)],
) -> BriefOut:
    try:
        brief = await generate_brief(
            session,
            runtime,
            org=principal.org,
            opportunity_id=opportunity_id,
            user_id=principal.user.id,
            idempotency_key=f"{principal.org.id}:{idempotency_key}",
        )
    except InsufficientCreditsError as exc:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            f"크레딧이 모자라요. 지금 {exc.balance}개 있고 {exc.needed}개가 필요해요",
        ) from exc
    except OpportunityNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "이 사업을 찾을 수 없어요") from exc
    return BriefOut.model_validate(brief)
