"""영업 브리핑 (sales brief) — a paid (credit-metered) one-page sales brief for one opportunity.

The brief is generated from *facts assembled by code* (signals with dated evidence, the buying
institution's recent purchase history, the vendor profile). The model writes; it does not
research — so every claim in the brief traces back to a stored signal.

Billing: credits are debited in the same transaction that stores the brief, keyed by the
client's idempotency key. If generation fails, the transaction rolls back and nothing is
charged; a double-clicked button returns the already-generated brief.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.ledger import InsufficientCreditsError, apply_credits
from app.billing.plans import BRIEF_CREDIT_COST
from app.clock import today_kst
from app.db.models import (
    Brief,
    CompanyProfile,
    InstitutionRow,
    Opportunity,
    OpportunitySignal,
    Organization,
    Signal,
)
from app.domain.krw import format_krw
from app.domain.stages import STAGE_LABEL, Stage
from app.domain.timing import month_span, remaining_window
from app.llm.prompts import BriefFacts, BriefSignal, PastTender
from app.runtime import Runtime

__all__ = [
    "InsufficientCreditsError",
    "OpportunityNotFoundError",
    "build_facts",
    "generate_brief",
    "template_brief",
]


class OpportunityNotFoundError(LookupError):
    pass


# Template-mode advice by the stage the opportunity has reached: what a seller can still
# influence, and what can still go wrong. Written the way a colleague would say it.
_STAGE_ADVICE: dict[Stage, tuple[list[str], list[str]]] = {
    Stage.COUNCIL: (
        [
            "아직 예산에 편성되기 전이에요. 담당 부서에 다른 지자체 도입 사례와 대략적인 비용을 먼저 건네면, "
            "기본계획이나 예산 요구안의 범위를 같이 잡을 수 있어요.",
            "의원이 꺼낸 문제(민원, 불편)를 제안서 첫 문단에 그대로 이어 주세요. 담당자가 내부 보고서에 옮겨 쓰기 쉬워져요.",
        ],
        [
            "본예산이나 추경 심의에서 빠지거나 깎일 수 있어요. 다음 예산서가 나오면 편성됐는지부터 확인해 보세요."
        ],
    ),
    Stage.BUDGET: (
        [
            "예산서에 세부사업명과 금액이 올라갔어요. 이 금액에 맞춘 구성안과 납품 실적을 챙겨서, "
            "발주계획이 나오기 전에 담당자를 만나 보세요.",
            "규격에 꼭 들어갔으면 하는 기능이 있다면 지금 기술 자료로 전달해야 해요. 사전규격이 나오면 늦어요.",
        ],
        ["발주 방식(협상, 제한경쟁)과 시기는 아직 바뀔 수 있어요."],
    ),
    Stage.ORDER_PLAN: (
        [
            "발주계획이 공개됐어요. 사전규격이 나오기 전에 규격 초안에 낼 의견을 준비하고, 담당자와 일정을 맞춰 보세요.",
            "참가 자격(실적, 인증)을 미리 따져 보고, 모자라면 컨소시엄 파트너를 찾아 두세요.",
        ],
        ["발주 시기가 한 분기씩 밀리는 일이 흔해요."],
    ),
    Stage.PRESPEC: (
        [
            "사전규격 의견등록 기간 안에 규격서를 꼼꼼히 보고, 특정 제품에만 유리한 조항이나 지나친 실적 요건이 "
            "있으면 의견을 내세요.",
            "입찰공고까지 몇 주 안 남았어요. 제안서 뼈대, 실적 증빙, 컨소시엄 구성을 이때 끝내 두세요.",
        ],
        ["사전규격 의견에 따라 규격이나 예산이 바뀌고, 공고가 늦어질 수도 있어요."],
    ),
    Stage.BID: (
        [
            "공고가 났어요. 제안요청서의 평가 배점과 제출 서류부터 확인하고, 마감일에서 거꾸로 일정을 짜 보세요."
        ],
        ["이제는 규격을 바꿀 수 없어요. 가격과 제안서 완성도로 승부해야 해요."],
    ),
    Stage.AWARD: (
        [
            "계약까지 끝난 사업이에요. 누가 얼마에 가져갔는지 적어 두면 다음 제안 가격을 잡을 때 도움이 돼요.",
            "유지보수나 다른 지역으로 넓히는 후속 사업을 노려 보세요. 이 기관의 다음 회의록과 예산서를 계속 지켜보면 돼요.",
        ],
        ["이번 사업에는 더 참여할 수 없어요."],
    ),
}

# How a council answer reads in the brief's timeline.
_SAID = {
    "committed": "'반영하겠다'고 답했어요.",
    "planned": "추진하겠다는 계획을 밝혔어요.",
    "reviewing": "'검토하겠다'는 정도였어요.",
    "declined": "'어렵다'고 답했어요.",
}
_WEAK = ("reviewing", "declined")


def _dot(d: date) -> str:
    return f"{d:%Y.%m.%d}"


async def build_facts(
    session: AsyncSession, opp: Opportunity, org_id: int, *, today: date | None = None
) -> BriefFacts:
    inst = await session.get(InstitutionRow, opp.institution_code) if opp.institution_code else None
    signals = (
        await session.scalars(
            select(Signal)
            .join(OpportunitySignal, OpportunitySignal.signal_id == Signal.id)
            .where(OpportunitySignal.opportunity_id == opp.id)
            .order_by(Signal.observed_at)
        )
    ).all()
    history = (
        await session.scalars(
            select(Opportunity)
            .where(
                Opportunity.institution_code == opp.institution_code,
                Opportunity.id != opp.id,
                Opportunity.bid_published_at.is_not(None),
            )
            .order_by(Opportunity.bid_published_at.desc())
            .limit(8)
        )
    ).all()
    profile = await session.get(CompanyProfile, org_id)
    today = today or today_kst()
    window_start, window_end, passed = remaining_window(
        opp.bid_window_start, opp.bid_window_end, today, published=opp.bid_published_at
    )
    if passed:  # keep the forecast that was missed, flagged, rather than a shifted one
        window_start, window_end = opp.bid_window_start, opp.bid_window_end
    return BriefFacts(
        today=today,
        title=opp.title,
        institution=inst.name if inst else None,
        department=opp.department,
        stage=Stage(opp.stage),
        status=opp.status,
        est_budget_krw=opp.est_budget_krw,
        window_start=window_start,
        window_end=window_end,
        bid_published_at=opp.bid_published_at,
        best_commitment=opp.best_commitment,
        conversion_prob=opp.conversion_prob,
        window_passed=passed,
        signals=tuple(
            BriefSignal(
                observed_at=s.observed_at,
                stage=Stage(s.stage),
                title=s.title,
                budget_krw=s.budget_krw,
                commitment=s.commitment,
                quote=str(next((e.get("quote", "") for e in s.evidence if e.get("found")), ""))[
                    :300
                ],
            )
            for s in signals
        ),
        history=tuple(
            PastTender(h.bid_published_at, h.title, h.est_budget_krw)
            for h in history
            if h.bid_published_at
        ),
        profile=(
            (
                f"- 소개: {profile.description or '-'}",
                f"- 주력 키워드: {', '.join(profile.keywords) or '-'}",
                f"- 선호 예산 범위: {profile.budget_min or '-'} ~ {profile.budget_max or '-'}",
            )
            if profile
            else ()
        ),
    )


def template_brief(facts: BriefFacts) -> str:
    """Deterministic brief used when no LLM is configured or the call fails. It reads the same
    facts the model would get, and says nothing the facts do not."""
    who = " ".join(x for x in (facts.institution, facts.department) if x)
    stage_label = STAGE_LABEL[facts.stage]
    span = month_span(facts.window_start, facts.window_end) if facts.window_start else None
    if facts.tender_out:
        on = _dot(facts.bid_published_at) if facts.bid_published_at else None
        timing = f"입찰공고는 {on}에 나왔어요." if on else "입찰공고는 이미 나왔어요."
        when = f"- 입찰공고: {on or '날짜 미상'}"
    elif span and facts.window_passed:
        timing = f"예상했던 입찰 시기({span})가 지났는데 아직 공고는 안 나왔어요."
        when = f"- 입찰 예상 시기: {span} (지났지만 아직 공고 없음)"
    elif span:
        timing = f"입찰은 {span}쯤 나올 것으로 보고 있어요."
        when = f"- 입찰 예상 시기: {span}"
    else:
        timing = "입찰 시기는 아직 가늠하기 어려워요."
        when = "- 입찰 시기: 아직 가늠하기 어려워요."
    budget = format_krw(facts.est_budget_krw) if facts.est_budget_krw else None

    actions, risks = _STAGE_ADVICE.get(facts.stage, _STAGE_ADVICE[Stage.BID])
    council = [s for s in facts.signals if s.stage is Stage.COUNCIL]
    if council and council[-1].commitment in _WEAK:
        risks = [
            *risks,
            "가장 최근 발언이 확약은 아니었어요. 다음 회기 회의록과 예산서를 꼭 확인해 보세요.",
        ]

    summary = f"{who + '의 ' if who else ''}「{facts.title}」 건이에요. 지금은 {stage_label} 단계예요. {timing}"
    if budget:
        summary += f" 예산은 {budget}으로 잡혀 있어요."

    timeline: list[str] = []
    for s in facts.signals:
        head = f"- **{_dot(s.observed_at)} · {STAGE_LABEL[s.stage]}** — {s.title}"
        if s.budget_krw:
            head += f" ({format_krw(s.budget_krw)})"
        said = _SAID.get(s.commitment or "") if s.stage is Stage.COUNCIL else None
        timeline.append(f"{head}. {said}" if said else head)
        # Quote what people said. Budget rows and procurement records are tables and field
        # dumps; the line above already carries their date, stage and amount.
        if s.stage is Stage.COUNCIL and s.one_line_quote.strip("…").strip():
            timeline.append(f"  > 「{s.one_line_quote}」")

    money = [
        f"- 추정 예산: {budget} (가장 진행된 단계의 문서 기준)"
        if budget
        else "- 추정 예산: 아직 금액이 나온 문서가 없어요.",
        when,
    ]
    if not facts.tender_out:  # once the tender is out there is nothing left to estimate
        money.append(
            f"- 공고로 이어질 가능성: {facts.conversion_prob:.0%} 정도로 봐요. 지금 단계와 의회 답변 "
            "수준, 같은 사업을 가리키는 문서 수로 매긴 추정치예요."
        )
    if facts.department:
        meet = [f"- {who}. 문서에 담당으로 나온 부서라서 여기부터 연락해 보세요."]
    elif who:
        meet = [
            f"- {who}. 담당 부서는 아직 문서에 안 나왔어요. 다음 회의록이나 예산서에서 부서명을 확인해 보세요."
        ]
    else:
        meet = ["- 수요 기관을 아직 특정하지 못했어요."]
    past = [
        f"- {_dot(h.published_at)} · {h.title} ({format_krw(h.budget_krw) if h.budget_krw else '금액 미상'})"
        for h in facts.history
    ] or ["- 아직 모아 둔 발주 이력이 없어요."]

    return "\n".join(
        [
            "## 한 줄 요약",
            summary,
            "",
            "## 지금까지의 경과",
            *(timeline or ["- 아직 잡힌 신호가 없어요."]),
            "",
            "## 예산과 시기",
            *money,
            "",
            "## 누구를 만나야 하나",
            *meet,
            "",
            "## 제안 전략",
            *(f"- {a}" for a in actions),
            "",
            "## 리스크",
            *(f"- {r}" for r in risks),
            "",
            "## 참고: 이 기관의 최근 발주",
            *past,
        ]
    )


async def generate_brief(
    session: AsyncSession,
    runtime: Runtime,
    *,
    org: Organization,
    opportunity_id: int,
    user_id: int,
    idempotency_key: str,
) -> Brief:
    existing = await session.scalar(select(Brief).where(Brief.idempotency_key == idempotency_key))
    if existing is not None:
        return existing
    # Anchor the facts before generation. Automatic link writers use this same row lock;
    # a paid report must never finish against an identity regrouped while the model ran.
    opp = await session.get(
        Opportunity, opportunity_id, with_for_update=True, populate_existing=True
    )
    if opp is None:
        raise OpportunityNotFoundError(opportunity_id)
    if org.credit_balance < BRIEF_CREDIT_COST:
        raise InsufficientCreditsError(org.credit_balance, BRIEF_CREDIT_COST)
    facts = await build_facts(session, opp, org.id)
    markdown, model = await runtime.llm.brief(session, facts)
    from app.pipeline.link_state import capture_customer_anchor

    # The identity lock kept this core stable during generation. Persist only now so a
    # model wait does not hold an anchor-table write lock against other institutions.
    await capture_customer_anchor(session, opp.id)
    brief = Brief(
        org_id=org.id,
        opportunity_id=opp.id,
        content_md=markdown,
        model=model,
        credits_spent=BRIEF_CREDIT_COST,
        idempotency_key=idempotency_key,
    )
    session.add(brief)
    await session.flush()
    await apply_credits(
        session,
        org_id=org.id,
        delta=-BRIEF_CREDIT_COST,
        reason="brief",
        idempotency_key=f"brief:{idempotency_key}",
        ref_type="brief",
        ref_id=str(brief.id),
        actor_user_id=user_id,
    )
    return brief


def brief_cache_date() -> date:
    return today_kst()
