"""Different projects in the same speech must not share their money or timing."""

from datetime import date

import pytest

from app.domain.grounding import locate_quote
from app.llm.prompts import ChunkContext
from app.llm.providers.heuristic import HeuristicProvider
from app.llm.schemas import ExtractedSignal


async def extract(text: str) -> list[ExtractedSignal]:
    ctx = ChunkContext("council_minutes", "예산 심의", "가상시", date(2026, 10, 1), [], text, 2027)
    return (await HeuristicProvider().extract(ctx)).value.signals


async def test_separate_future_projects_keep_local_amounts_and_years() -> None:
    text = (
        "○정보화과장 김서준  2027년 민원 플랫폼 구축에 총 7억 원을 사용할 계획입니다. "
        "별도 행정망 서버 교체에 총 9억 원을 2028년에 사용할 계획입니다."
    )
    signals = await extract(text)
    assert len(signals) == 2
    assert [(s.budget_krw, s.expected_year) for s in signals] == [
        (700_000_000, 2027),
        (900_000_000, 2028),
    ]
    assert "민원 플랫폼 구축" in signals[0].title
    assert "행정망 서버 교체" in signals[1].title
    assert all(locate_quote(text, q).method == "exact" for s in signals for q in s.evidence)


async def test_completed_platform_cannot_replace_future_tools_purchase() -> None:
    signals = await extract(
        "○정보화과장 김서준  작년에 데이터 통합 플랫폼을 구축했습니다. "
        "기존 플랫폼 유지보수비는 4억 원입니다. "
        "내년에는 생성형 AI 업무 도구 구입을 추진할 계획입니다."
    )
    assert len(signals) == 1
    assert signals[0].title == "생성형 AI 업무 도구 구입"
    assert signals[0].category == "ai_data"
    assert signals[0].budget_krw is None
    assert signals[0].expected_year == 2027


async def test_explicit_purchase_with_unknown_category_remains_review_candidate() -> None:
    signals = await extract(
        "○총무과장 김서준  회의실 탁상시계 구입에 700만 원을 내년에 편성할 예정입니다."
    )
    assert len(signals) == 1
    assert "탁상시계 구입" in signals[0].title
    assert signals[0].budget_krw == 7_000_000


async def test_question_project_precedes_generic_answer_system_name() -> None:
    signals = await extract(
        "○위원 이민우  AI 상담 플랫폼 구축 예산은 얼마입니까?\n"
        "○정보화과장 김서준  이 예산은 3억 원입니다. "
        "챗봇과 보이스봇 두 가지 시스템 구축을 내년에 추진할 계획입니다."
    )
    assert len(signals) == 1
    assert "AI 상담 플랫폼" in signals[0].title
    assert signals[0].budget_krw == 300_000_000


@pytest.mark.parametrize(
    "statement",
    [
        "태양광 설치비를 내년에 가구당 200만 원씩 지원할 계획입니다.",
        "작년에 통합관제시스템 구축을 완료했습니다. 지금은 운영 중입니다.",
        "장학기금 설치 및 운용 조례를 내년부터 시행할 계획입니다.",
    ],
)
async def test_nonpurchase_or_completed_work_has_no_future_signal(statement: str) -> None:
    assert await extract("○행정과장 김서준  " + statement) == []


async def test_later_project_cancellation_cannot_be_ignored() -> None:
    signals = await extract(
        "○정보화과장 김서준  민원 플랫폼 구축을 내년에 추진할 계획입니다. "
        "그러나 해당 사업은 재정 부족으로 취소되었습니다."
    )
    assert all(s.commitment not in {"committed", "planned"} for s in signals)


async def test_member_purchase_proposal_cannot_borrow_unrelated_official_intent() -> None:
    assert (
        await extract(
            "○위원 이민우  내년에 통학버스 구입을 해야 합니다.\n"
            "○교육과장 김서준  장학금 지원을 확대할 계획입니다."
        )
        == []
    )


async def test_separate_questions_cannot_exchange_commitment_or_budget() -> None:
    signals = await extract(
        "○위원 이민우  스마트폴 설치 계획은요?\n"
        "○도시과장 김서준  협의가 되는 대로 추진 여부를 판단하겠습니다.\n"
        "○위원 이민우  CCTV 선별관제는요?\n"
        "○도시과장 김서준  선별관제는 내년에 발주할 예정이며 예산은 2억 원입니다."
    )
    assert len(signals) == 2
    assert signals[0].commitment == "reviewing"
    assert signals[0].budget_krw is None
    assert "선별관제" in signals[1].title
    assert signals[1].commitment == "committed"
    assert signals[1].budget_krw == 200_000_000


async def test_old_building_date_is_not_future_purchase_date() -> None:
    signals = await extract(
        "○시설과장 김서준  청사가 2003년에 개청한 뒤 교체하지 않아 노후했습니다. "
        "청사 배수로 그레이팅 교체를 추진할 계획입니다."
    )
    assert len(signals) == 1
    assert signals[0].expected_year is None


async def test_enumerated_projects_keep_distinct_commitment_and_budget() -> None:
    signals = await extract(
        "○안전과장 김서준  세 가지를 준비하고 있습니다. "
        "첫째, 스마트 횡단보도는 1억 원을 확보해서 내년에 발주합니다. "
        "둘째, 통학로 비상벨 설치는 2028년 추진할 계획입니다. "
        "셋째, 보행자 신호 시스템은 검토해 보겠습니다."
    )
    assert len(signals) == 3
    assert [s.commitment for s in signals] == ["committed", "planned", "reviewing"]
    assert [s.budget_krw for s in signals] == [100_000_000, None, None]
