"""Constructed table formats exercise row boundaries, not golden-case phrases."""

from datetime import date

import pytest

from app.domain.taxonomy import Category
from app.llm.prompts import ChunkContext
from app.llm.providers.heuristic import HeuristicProvider
from app.llm.schemas import ExtractedSignal
from app.parsing.chunking import chunk_budget


async def _signals(text: str) -> list[ExtractedSignal]:
    return (
        await HeuristicProvider().extract(
            ChunkContext(
                "budget_book", "2027년 예산서", "해솔시", date(2026, 12, 10), [], text, 2027
            )
        )
    ).value.signals


@pytest.mark.parametrize(
    "text",
    [
        "(단위: 천원) 세부사업 | 2027년 예산액 | 2026년 예산액 | 증감\n"
        "세부사업: 공공도서관 리모델링 | 420,000 | 200,000 | 220,000\n"
        "  ○ 지붕 보수공사 1식",
        "| 세부사업 | 2027년 예산액 | 2026년 예산액 | 증감 |\n"
        "| :--- | ---: | ---: | ---: |\n"
        "| 공공도서관 리모델링 | 420,000 | 200,000 | 220,000 |\n"
        "| 401 시설비및부대비 | 420,000 | 200,000 | 220,000 |\n"
        "| 01 시설비 | 420,000 | 200,000 | 220,000 |",
    ],
)
async def test_markdown_budget_rows_do_not_extract_year_headers_or_account_rows(text: str) -> None:
    chunks = chunk_budget(text)
    assert len(chunks) == 1
    assert "공공도서관 리모델링" in chunks[0].text.splitlines()[0]
    assert all(text[c.char_start : c.char_end] == c.text for c in chunks)
    signals = await _signals(text)
    assert [(s.title, s.category, s.budget_krw) for s in signals] == [
        ("공공도서관 리모델링", Category.FACILITY, 420_000_000)
    ]
    assert signals[0].evidence[0] in text
    assert "2027년 예산액" not in signals[0].evidence[0]


async def test_adjacent_labeled_projects_keep_own_amounts_evidence_and_keywords() -> None:
    text = (
        "세부사업: 계약직 직원 보수 94,000 90,000 4,000\n"
        "  ○ 직원 4명 인건비\n"
        "세부사업: 하천 침수 감시시스템 구축 610,000 0 610,000\n"
        "  ○ AI 영상분석 장비\n"
        "세부사업: 마을역사 전시 콘텐츠 제작 170,000 0 170,000\n"
        "  ○ 실감형 영상 콘텐츠"
    )
    signals = await _signals(text)
    assert [(s.title, s.category, s.budget_krw) for s in signals] == [
        ("하천 침수 감시시스템 구축", Category.SAFETY_CCTV, 610_000_000),
        ("마을역사 전시 콘텐츠 제작", Category.TOURISM_CULTURE, 170_000_000),
    ]
    assert all(s.title in s.evidence[0] for s in signals)
    assert "콘텐츠" not in signals[0].keywords
    assert "침수" not in signals[1].keywords


async def test_unlabeled_real_table_excludes_subtotals_and_account_rows() -> None:
    text = (
        "부서: 문화과\n"
        "문화과 900,000 500,000 400,000\n"
        "문화시설 개선 900,000 500,000 400,000\n"
        "도서관 보수공사 450,000 0 450,000\n"
        "401 시설비및부대비 450,000 0 450,000\n"
        "01 시설비 450,000 0 450,000\n"
        "  ○ 도서관 시설개선\n"
        "전시관 홈페이지 개편 80,000 0 80,000\n"
        "207 연구개발비 80,000 0 80,000\n"
        "02 전산개발비 80,000 0 80,000\n"
    )
    signals = await _signals(text)
    assert [(s.title, s.category) for s in signals] == [
        ("도서관 보수공사", Category.FACILITY),
        ("전시관 홈페이지 개편", Category.PUBLIC_SW),
    ]
    assert all(s.department == "문화과" for s in signals)


@pytest.mark.parametrize(("unit", "amount"), [("백만원", 510_000_000), ("원", 510)])
async def test_unit_header_is_not_a_project_and_applies_to_following_rows(
    unit: str, amount: int
) -> None:
    signals = await _signals(f"(단위: {unit})\n세 부 사 업 : CCTV 설치\t510\t0\t510")
    assert len(signals) == 1
    assert signals[0].title == "CCTV 설치"
    assert signals[0].budget_krw == amount


@pytest.mark.parametrize(
    "text",
    [
        "세부사업 | 2027년 예산액 | 2026년 예산액 | 증감",
        "401 시설비및부대비 120,000 0 120,000",
        "01 시설비 120,000 0 120,000",
        "세부사업: 401 시설비및부대비 120,000 0 120,000",
    ],
)
async def test_table_headers_and_account_labels_alone_never_become_projects(text: str) -> None:
    assert await _signals(text) == []


async def test_employee_facility_repairs_are_not_employee_wages() -> None:
    signals = await _signals("세부사업: 근로자 복지관 보수공사 240,000 0 240,000")
    assert [(s.title, s.category) for s in signals] == [
        ("근로자 복지관 보수공사", Category.FACILITY)
    ]
