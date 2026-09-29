"""Budget purpose and title outrank account labels and incidental technology words.

Named historical titles come from docs/real-data-budget.md §8.5. The detail lines in
these tests are constructed contamination cases, not reconstructed original source text.
"""

from datetime import date

import pytest

from app.domain.taxonomy import Category
from app.llm.prompts import ChunkContext
from app.llm.providers.heuristic import HeuristicProvider
from app.llm.schemas import ExtractedSignal
from app.pipeline.process import check_signal


async def _extract(title: str, details: str = "") -> tuple[ExtractedSignal, str]:
    text = f"{title} 300,000 0 300,000\n{details}"
    context = ChunkContext(
        "budget_book",
        "2026년도 예산서",
        "성남시",
        date(2025, 12, 20),
        ["부서: 행정과"],
        text,
        fiscal_year=2026,
    )
    result = await HeuristicProvider().extract(context)
    selected = [signal for signal in result.value.signals if signal.title == title]
    assert len(selected) == 1
    return selected[0], text


@pytest.mark.parametrize(
    ("title", "details", "category"),
    [
        (
            "성남시 청소년 교향악 페스티벌",
            "401 시설비및부대비\n○교육용 인쇄물",
            Category.TOURISM_CULTURE,
        ),
        (
            "지역사회 통합건강증진사업(구강보건)",
            "301 일반보전금\n○학교 교육자료",
            Category.WELFARE_CARE,
        ),
        ("개별주택가격 조사·산정", "401 시설비및부대비\n○시설 공사 설계 정비", Category.OTHER),
        ("전국동시지방선거 추진", "401 시설비및부대비\n○시설 공사 설계 정비", Category.OTHER),
        ("공공도서관 리모델링", "○홈페이지 정보시스템 클라우드 전환", Category.FACILITY),
        ("노인복지관 홈페이지 개편", "○복지 서비스 안내", Category.PUBLIC_SW),
        ("AI·IoT 기반 구강보건 건강증진사업", "○교육자료 제작", Category.WELFARE_CARE),
        ("IoT 기반 고독사 예방사업", "○사물인터넷 장비 구입", Category.WELFARE_CARE),
        ("어린이보호구역 AI 과속단속카메라 설치", "○AI 영상 분석 서버", Category.SAFETY_CCTV),
        ("해수욕장 AI 이안류 감시시스템 구축", "○AI 영상 분석 서버", Category.SAFETY_CCTV),
        ("스마트 버스정류장 조성", "○버스 교통 안내", Category.SMART_CITY),
        ("AI 코딩 교육실 조성", "○인공지능 소프트웨어", Category.EDUCATION),
        ("가로수 및 녹지대 병해충 방제", "401 시설비및부대비", Category.ENERGY_ENV),
        ("스마트 LED 가로등 교체", "401 시설비및부대비", Category.ENERGY_ENV),
        ("도로명 안내표지판 정비공사", "○행정 안내", Category.FACILITY),
        ("다문화사회 이해교육", "○문화행사 홍보", Category.EDUCATION),
        ("메타버스 관광플랫폼 구축", "○가상관광 콘텐츠", Category.TOURISM_CULTURE),
    ],
)
async def test_budget_classification_follows_the_named_project(
    title: str, details: str, category: Category
) -> None:
    signal, _ = await _extract(title, details)
    assert signal.category is category
    assert signal.title == title
    assert signal.budget_krw == 300_000_000
    assert signal.expected_year == 2026


@pytest.mark.parametrize(
    ("title", "details"),
    [
        ("AI 분석 기반 신규사업", "401 시설비및부대비"),
        ("태양광과 스마트쉘터 복합 설치", ""),
        ("장비 구입", "○CCTV 설치\n○구강보건 장비 구입"),
        ("신규 사업", "401 시설비및부대비\n01 시설비\n부서: 정보통신과"),
        ("신규 사업", "세부사업: CCTV 설치 300,000 0 300,000\n○CCTV 설치"),
        ("사업 추진", "고독사 예방사업 300,000 0 300,000\n○고독사 예방"),
    ],
)
async def test_weak_or_conflicting_classification_stays_other_and_reviewable(
    title: str, details: str
) -> None:
    signal, text = await _extract(title, details)
    assert signal.category is Category.OTHER
    checked = check_signal(
        signal,
        text=text,
        doc_type="budget_book",
        reference_date=date(2025, 12, 20),
        fiscal_year=2026,
        table_unit=1000,
        min_score=88,
    )
    assert checked.report.verdict == "needs_review"
    assert "low_confidence" in checked.report.issues


async def test_specific_detail_can_resolve_a_genuinely_generic_title() -> None:
    signal, _ = await _extract("장비 구입", "405 자산취득비\n○어린이보호구역 CCTV 구입")
    assert signal.category is Category.SAFETY_CCTV


async def test_unknown_named_program_does_not_borrow_a_detail_category() -> None:
    signal, _ = await _extract("전국동시지방선거 추진", "○청사 CCTV 설치")
    assert signal.category is Category.OTHER


async def test_budget_keywords_do_not_borrow_another_projects_technology() -> None:
    signal, _ = await _extract("공공도서관 리모델링", "○홈페이지 정보시스템 클라우드 전환")
    assert set(signal.keywords) == {"도서관", "리모델링"}


@pytest.mark.parametrize(
    "title",
    [
        "오리공원 물놀이장 설치공사",
        "탄천 보행교 설치공사",
        "탄천 보행교 설치 공사",
        "달빛천 교량 설치 공사",
        "솔마루 어린이놀이터 조성공사",
        "주민 산책로 개설공사",
    ],
)
async def test_named_civil_construction_target_is_grounded_and_link_eligible(title: str) -> None:
    # The first three names reproduce failed pre-existing integration fixtures. The
    # remaining names independently exercise construction assets/actions and spacing.
    from app.parsing.chunking import chunk_budget

    text = (
        "부서: 시설과\n정책: 공공시설 확충\n단위: 생활시설 (단위:천원)\n"
        f"{title} 1,000,000 0 1,000,000\n"
        "401 시설비및부대비 1,000,000 0 1,000,000\n"
        "01 시설비 1,000,000 0 1,000,000\n"
        f" ○{title}\n1,000,000\n"
    )
    chunks = chunk_budget(text)
    assert len(chunks) == 1
    chunk = chunks[0]
    result = await HeuristicProvider().extract(
        ChunkContext(
            "budget_book",
            "2026년 예산서",
            "성남시",
            date(2026, 6, 18),
            chunk.labels,
            chunk.text,
            fiscal_year=2026,
        )
    )
    assert len(result.value.signals) == 1
    signal = result.value.signals[0]
    assert signal.title == title
    assert signal.category is Category.FACILITY
    assert signal.budget_krw == 1_000_000_000
    checked = check_signal(
        signal,
        text=text,
        doc_type="budget_book",
        reference_date=date(2026, 6, 18),
        fiscal_year=2026,
        table_unit=1000,
        min_score=88,
    )
    assert checked.report.verdict == "accepted"


@pytest.mark.parametrize(
    ("title", "category"),
    [
        ("도심 CCTV 설치공사", Category.SAFETY_CCTV),
        ("공원 스마트쉘터 설치공사", Category.SMART_CITY),
        ("공원 정보시스템 설치공사", Category.PUBLIC_SW),
        ("신규 설치공사", Category.OTHER),
    ],
)
async def test_generic_construction_word_never_overrides_purchase_target(
    title: str, category: Category
) -> None:
    signal, _ = await _extract(title, "401 시설비및부대비\n○공사 설계 시설 정비")
    assert signal.category is category
    if category is Category.OTHER:
        assert signal.confidence == 0.35
