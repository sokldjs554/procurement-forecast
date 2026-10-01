"""Purchase attribution on synthetic formats independent of the frozen real set."""

from datetime import date

import pytest

from app.domain.grounding import locate_quote
from app.domain.krw import detect_table_unit
from app.llm.prompts import ChunkContext
from app.llm.providers.heuristic import HeuristicProvider
from app.llm.schemas import ExtractedSignal
from app.parsing.chunking import chunk_budget
from app.pipeline.process import check_signal
from app.pipeline.triage import triage_chunk


async def extract(text: str, kind: str = "council_minutes") -> list[ExtractedSignal]:
    ctx = ChunkContext(kind, "2027년도 예산안", "가상군", date(2026, 10, 1), [], text, 2027)
    return (await HeuristicProvider().extract(ctx)).value.signals


async def test_exchange_budget_excludes_hypothetical_expansion_cost() -> None:
    text = (
        "○위원 이민우  민원 플랫폼 구축 예산은 얼마입니까?\n"
        "○정보화과장 김서준  이 예산에서 3억 원을 세웠습니다. "
        "모든 분야를 구축한다고 하면 12억 원이 필요합니다. "
        "내년에는 일부만 구축할 계획입니다."
    )
    signals = await extract(text)
    assert len(signals) == 1
    assert signals[0].budget_krw == 300_000_000
    assert any("3억 원" in q for q in signals[0].evidence)


async def test_exchange_unattributed_multiple_amounts_stay_unknown() -> None:
    signals = await extract(
        "○정보화과장 김서준  민원 플랫폼 구축을 내년에 추진할 계획입니다. "
        "견적은 3억 원입니다. 다른 견적은 5억 원입니다."
    )
    assert len(signals) == 1
    assert signals[0].budget_krw is None


async def test_exchange_explicit_total_is_not_confused_with_funding_component() -> None:
    signals = await extract(
        "○정보화과장 김서준  국비 2억 원을 확보했고 시비를 더해 "
        "총 7억 원 규모로 민원 플랫폼 구축을 올해 추진할 계획입니다."
    )
    assert len(signals) == 1
    assert signals[0].budget_krw == 700_000_000


async def test_exchange_multiple_project_totals_remain_ambiguous() -> None:
    signals = await extract(
        "○정보화과장 김서준  민원 플랫폼 구축에 총 7억 원을 사용할 계획입니다. "
        "별도 관제시스템 구축에도 총 9억 원을 사용할 계획입니다."
    )
    assert len(signals) == 1
    assert signals[0].budget_krw is None


@pytest.mark.parametrize(
    "cost_statement",
    ["공사비 6억 원은 이미 확보했습니다.", "설계는 이미 끝났고 공사비는 6억 원입니다."],
)
async def test_completed_preparation_does_not_erase_future_purchase_budget(
    cost_statement: str,
) -> None:
    signals = await extract(
        "○시설과장 김서준  주민센터 리모델링 공사를 내년에 발주할 예정입니다. " + cost_statement
    )
    assert len(signals) == 1
    assert signals[0].budget_krw == 600_000_000


async def test_numbered_review_sections_preserve_current_budget_and_boundaries() -> None:
    text = (
        "1) 주민센터 리모델링 공사(신규)(p.70)\n"
        "- 사업내용: 노후 시설 리모델링\n- 소요재원\n(단위: 천원)\n"
        "재원별 예산액 전년도당초예산액 비교증감\n"
        "계 125,400 200,000 -74,600\n자체재원 125,400 200,000 -74,600\n"
        "- 의견: 약 1억원 규모의 공사임.\n"
        "2) 공무원 수당(신규)(p.71)\n- 소요재원\n(단위: 천원)\n"
        "재원별 예산액 전년도당초예산액 비교증감\n계 900,000 0 900,000\n"
    )
    signals = await extract(text, "budget_book")
    assert len(signals) == 1
    assert signals[0].title == "주민센터 리모델링 공사"
    assert signals[0].budget_krw == 125_400_000
    assert signals[0].commitment == "planned"
    assert all(locate_quote(text, q).method == "exact" for q in signals[0].evidence)


async def test_numbered_review_does_not_guess_without_column_header() -> None:
    text = "1) 주민센터 리모델링 공사(신규)(p.70)\n(단위: 천원)\n계 125,400 200,000\n"
    assert await extract(text, "budget_book") == []


async def test_numbered_review_survives_production_chunking_with_offsets() -> None:
    text = (
        "검토보고서\n1) 주민센터 리모델링 공사(신규)(p.70)\n"
        "- 소요재원\n(단위: 천원)\n재원별 예산액 전년도당초예산액 비교증감\n"
        "계 125,400 0 125,400\n"
        "2) 행정망 서버 교체(신규)(p.71)\n"
        "- 소요재원\n(단위: 천원)\n재원별 예산액 전년도당초예산액 비교증감\n"
        "계 88,000 0 88,000\n"
    )
    chunks = chunk_budget(text)
    assert len(chunks) == 2
    signals = []
    for chunk in chunks:
        assert text[chunk.char_start : chunk.char_end] == chunk.text
        assert triage_chunk(chunk.text, kind=chunk.kind, threshold=0.35).passed
        signals.extend(await extract(chunk.text, "budget_book"))
    assert [(s.title, s.budget_krw) for s in signals] == [
        ("주민센터 리모델링 공사", 125_400_000),
        ("행정망 서버 교체", 88_000_000),
    ]


async def test_itemized_allocations_keep_each_asset_and_own_amount() -> None:
    text = (
        "○정보화과장 김서준  태블릿PC 구입에 1,200만 원, "
        "백업시스템 장비 교체에 3,400만 원을 계상하였습니다."
    )
    signals = await extract(text)
    assert len(signals) == 2
    assert [(s.title, s.budget_krw) for s in signals] == [
        ("태블릿PC 구입", 12_000_000),
        ("백업시스템 장비 교체", 34_000_000),
    ]
    assert all(s.commitment == "committed" for s in signals)
    assert all(locate_quote(text, q).method == "exact" for s in signals for q in s.evidence)
    assert triage_chunk(text, kind="statement", threshold=0.35).passed


async def test_allocation_dates_are_local_to_each_purchase() -> None:
    signals = await extract(
        "○정보화과장 김서준  2027년 행정정보시스템 구축에 1억 원, "
        "2028년 백업시스템 장비 교체에 2억 원을 계상하였습니다."
    )
    assert [s.expected_year for s in signals] == [2027, 2028]


@pytest.mark.parametrize(
    "statement",
    [
        "지난해 태블릿PC 구입에 1,200만 원을 계상하였습니다.",
        "재원이 확보되면 태블릿PC 구입에 1,200만 원을 계상하겠습니다.",
        "태블릿PC 구입에 1,200만 원을 계상하였으나 취소했습니다.",
    ],
)
async def test_historical_conditional_and_cancelled_allocations_abstain(statement: str) -> None:
    assert await extract("○정보화과장 김서준  " + statement) == []


async def test_cancelled_work_plan_is_not_a_planned_purchase() -> None:
    text = (
        "2. 행정망 장비 고도화\n❏ 사업개요\n◦ 사업기간: 2027년\n"
        "❏ 추진계획\n◦ 재정 부족으로 사업 취소\n❏ 소요예산\n◦ 80백만 원"
    )
    assert await extract(text, "budget_book") == []


@pytest.mark.parametrize("particle", ["은", "이", "도"])
async def test_work_plan_denial_with_particles_is_not_a_new_purchase(particle: str) -> None:
    text = (
        "2. 행정망 장비 고도화\n❏ 사업개요\n◦ 사업기간: 2025년\n"
        f"❏ 추진계획\n◦ 추가 도입 계획{particle} 없습니다.\n❏ 소요예산\n◦ 80백만 원"
    )
    assert await extract(text, "budget_book") == []


@pytest.mark.parametrize("speaker", ["○위원 이민우", "○전문위원 김서준", ""])
async def test_allocations_without_executive_provenance_are_not_asserted(speaker: str) -> None:
    assert await extract(f"{speaker}\n태블릿PC 구입에 1,200만 원을 계상하였습니다.") == []


async def test_completed_purchase_and_maintenance_are_not_new_allocations() -> None:
    text = (
        "○정보화과장 김서준  지난해 태블릿PC 구입에 1,200만 원을 집행 완료하였습니다. "
        "기존 백업시스템 유지보수비 3,400만 원을 계상하였습니다."
    )
    assert await extract(text) == []


async def test_plan_budget_is_unique_expense_not_history_amount() -> None:
    text = (
        "2. 생성형 AI 문서작성 도구 도입\n❏ 사업개요\n◦ 사업기간: 2027. 1.~12.\n"
        "❏ 추진실적\n◦ 2025년 시험 도입 9천만 원\n"
        "❏ 추진계획\n◦ 2027년 유료 계정 추가 도입\n❏ 소요예산\n◦ 18백만 원(군비)"
    )
    signals = await extract(text, "budget_book")
    assert len(signals) == 1
    assert signals[0].title == "생성형 AI 문서작성 도구 도입"
    assert signals[0].budget_krw == 18_000_000
    assert signals[0].commitment == "planned"
    assert signals[0].expected_year == 2027
    assert all(locate_quote(text, q).method == "exact" for q in signals[0].evidence)
    chunks = chunk_budget(text)
    assert len(chunks) == 1
    assert (await extract(chunks[0].text, "budget_book"))[0] == signals[0]
    assert text[chunks[0].char_start : chunks[0].char_end] == chunks[0].text


async def test_plan_with_multiple_costs_abstains_from_inventing_total() -> None:
    text = (
        "2. 행정망 장비 고도화\n❏ 사업개요\n◦ 사업기간: 2027년~2028년\n"
        "❏ 연차별 소요예산\n◦ 교체비 80백만 원\n◦ 운영비 12백만 원"
    )
    signals = await extract(text, "budget_book")
    assert len(signals) == 1
    assert signals[0].budget_krw is None


async def test_review_table_page_numbers_and_wrapped_delta_preserve_first_cell() -> None:
    text = (
        "<주요사업 예산 증감내역>\n(단위: 천원, %)\n사업명 예산액 기정액 비교증감\n"
        "31p\n(45p) 청사 회의실 의자 구입 17,000 - 17,000 순증\n"
        "32p\n(46p) 별관 방수공사 260,000\n- 260,000 순증\n"
        "33p\n(47p) 직원 격려금 30,000 - 30,000 순증"
    )
    signals = await extract(text, "budget_book")
    assert [(s.title, s.budget_krw) for s in signals] == [
        ("청사 회의실 의자 구입", 17_000_000),
        ("별관 방수공사", 260_000_000),
    ]
    assert all(locate_quote(text, q).method == "exact" for s in signals for q in s.evidence)
    chunks = chunk_budget(text)
    assert len(chunks) == 1
    assert await extract(chunks[0].text, "budget_book") == signals
    single = text[: text.index("32p")]
    chunk = chunk_budget(single)[0]
    assert triage_chunk(chunk.text, kind=chunk.kind, threshold=0.35).passed


async def test_review_table_preserves_preceding_unit_through_grounding() -> None:
    text = (
        "(단위: 백만원)\n<주요사업 예산 증감내역>\n사업명 예산액 기정액 비교증감\n"
        "(45p) 행정정보시스템 고도화 1,200 1,500 -300 20.0"
    )
    chunk = chunk_budget(text)[0]
    sig = (await extract(chunk.text, "budget_book"))[0]
    checked = check_signal(
        sig,
        text=chunk.text,
        doc_type="budget_book",
        reference_date=date(2026, 10, 1),
        fiscal_year=2027,
        table_unit=detect_table_unit(text) or 1000,
        min_score=88.0,
    )
    assert sig.budget_krw == 1_200_000_000
    assert checked.budget_krw == 1_200_000_000
    assert checked.report.verdict == "accepted"


async def test_numbered_subsection_is_not_a_new_project() -> None:
    text = (
        "2. 행정망 장비 고도화\n❏ 사업개요\n◦ 사업기간: 2027년\n"
        "❏ 추진계획\n1) 하반기 시스템 교체 추진\n❏ 소요예산\n◦ 80백만 원"
    )
    chunks = chunk_budget(text)
    assert len(chunks) == 1
    assert len(await extract(chunks[0].text, "budget_book")) == 1


async def test_adjacent_review_tables_have_disjoint_ranges_and_their_own_units() -> None:
    text = (
        "(단위: 백만원)\n<주요사업 예산 증감내역>\n사업명 예산액 기정액 비교증감\n"
        "(45p) 행정정보시스템 고도화 1,200 1,500 -300 20.0\n"
        "(단위: 천원)\n<주요사업 예산 증감내역>\n사업명 예산액 기정액 비교증감\n"
        "(46p) 회의실 의자 구입 17,000 - 17,000 순증"
    )
    chunks = chunk_budget(text)
    assert len(chunks) == 2
    assert chunks[0].char_end <= chunks[1].char_start
    signals = [s for c in chunks for s in await extract(c.text, "budget_book")]
    assert [s.budget_krw for s in signals] == [1_200_000_000, 17_000_000]
    checked = check_signal(
        signals[1],
        text=chunks[1].text,
        doc_type="budget_book",
        reference_date=date(2026, 10, 1),
        fiscal_year=2027,
        table_unit=detect_table_unit(text) or 1000,
        min_score=88.0,
    )
    assert checked.budget_krw == 17_000_000
