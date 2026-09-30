"""Synthetic regression cases for minutes attribution and maintenance-only statements."""

from datetime import date

import pytest

from app.domain.grounding import locate_quote, official_evidence_issue
from app.llm.prompts import ChunkContext
from app.llm.providers.heuristic import HeuristicProvider
from app.parsing.chunking import Chunk, chunk_minutes

PROJECT = "내년도 본예산에 정보시스템 구축 사업비 4억 원을 반영하겠습니다."


def _context(text: str, labels: list[str]) -> ChunkContext:
    return ChunkContext("council_minutes", "합성 회의", "가상시", date(2026, 9, 1), labels, text)


def _continuation(role: str) -> tuple[str, Chunk]:
    source = f"○{role} 김서준  " + "지금까지의 경과를 설명드립니다. " * 160 + PROJECT
    chunks = chunk_minutes(source)
    assert len(chunks) >= 2
    continuation = chunks[-1]
    assert "○" not in continuation.text
    assert PROJECT in continuation.text
    assert continuation.labels == [f"{role} 김서준"]
    return source, continuation


async def test_official_continuation_from_real_chunker_retains_verbatim_evidence() -> None:
    source, chunk = _continuation("정보화과장")
    signals = (await HeuristicProvider().extract(_context(chunk.text, chunk.labels))).value.signals

    assert len(signals) == 1
    signal = signals[0]
    assert signal.title == "정보시스템 구축"
    assert signal.commitment == "committed"
    assert signal.budget_krw == 400_000_000
    assert signal.expected_year == 2027
    assert signal.evidence == [PROJECT]
    assert source[chunk.char_start : chunk.char_end] == chunk.text
    checks = [locate_quote(chunk.text, quote) for quote in signal.evidence]
    assert all(check.found and check.method == "exact" for check in checks)
    assert (
        official_evidence_issue(
            source, checks, char_start=chunk.char_start, commitment=signal.commitment
        )
        is None
    )


@pytest.mark.parametrize("role", ["위원", "의원", "위원장", "부의장", "전문위원", "참고인"])
async def test_nonexecutive_continuations_do_not_gain_authority(role: str) -> None:
    _, chunk = _continuation(role)

    result = await HeuristicProvider().extract(_context(chunk.text, chunk.labels))

    assert result.value.signals == []


@pytest.mark.parametrize(
    "labels",
    [
        [],
        ["정보화과장"],
        ["부서: 정보화과장 김서준"],
        ["정보화과장 김서준 추가 설명"],
        ["정보화과장 김서준", "위원 이민우"],
        ["정보화과장 김서준", "참고인 박서연"],
        ["정보화과장 김서준", "환경국장 박서연"],
        ["정보화과장 김서준", "정보화과장 박서연"],
    ],
)
async def test_missing_malformed_or_ambiguous_labels_do_not_gain_authority(
    labels: list[str],
) -> None:
    result = await HeuristicProvider().extract(_context(PROJECT, labels))

    assert result.value.signals == []


@pytest.mark.parametrize("header", ["○위원 이민우  ", "○이민우위원  ", "○이민우 의원  "])
async def test_explicit_member_header_overrides_official_label(header: str) -> None:
    result = await HeuristicProvider().extract(_context(header + PROJECT, ["정보화과장 김서준"]))

    assert result.value.signals == []


async def test_explicit_official_header_overrides_member_label() -> None:
    result = await HeuristicProvider().extract(
        _context("○정보화과장 김서준  " + PROJECT, ["위원 이민우"])
    )

    assert len(result.value.signals) == 1
    assert result.value.signals[0].evidence == [PROJECT]


async def test_standalone_official_header_attributes_following_speech() -> None:
    source = "○정보화과장 김서준\n" + PROJECT
    chunks = chunk_minutes(source)
    assert len(chunks) == 1

    result = await HeuristicProvider().extract(_context(chunks[0].text, chunks[0].labels))

    assert len(result.value.signals) == 1
    assert result.value.signals[0].evidence == [PROJECT]


@pytest.mark.parametrize("role", ["위원", "의원", "위원장", "부의장", "전문위원", "참고인"])
async def test_standalone_nonexecutive_header_does_not_gain_authority(role: str) -> None:
    result = await HeuristicProvider().extract(
        _context(f"○{role} 김서준\n" + PROJECT, ["정보화과장 김서준"])
    )

    assert result.value.signals == []


async def test_label_does_not_attribute_prefix_before_an_explicit_speaker() -> None:
    result = await HeuristicProvider().extract(
        _context(PROJECT + "\n○위원 이민우  설명을 마치겠습니다.", ["정보화과장 김서준"])
    )

    assert result.value.signals == []


@pytest.mark.parametrize("maintenance", ["유지관리", "유지보수", "유지 관리"])
async def test_existing_system_maintenance_budget_reallocation_is_not_a_new_purchase(
    maintenance: str,
) -> None:
    text = (
        "○정보화과장 김서준  기존 주민센터에서 정보시스템을 운영하고 있습니다. "
        f"정보시스템 {maintenance} 용역은 매년 시행하고 있습니다. "
        "내년도 예산은 각 센터 예산을 줄여 본청 예산으로 통합 편성했습니다."
    )

    result = await HeuristicProvider().extract(_context(text, ["정보화과장 김서준"]))

    assert result.value.signals == []


@pytest.mark.parametrize(
    "project", ["무인민원발급기 설치", "모바일 앱 개발 사업", "공공도서관 건립 사업"]
)
async def test_new_purchase_is_retained_when_existing_maintenance_is_also_discussed(
    project: str,
) -> None:
    text = (
        "○정보화과장 김서준  기존 정보시스템 유지관리 용역은 계속 진행합니다. "
        f"내년도 본예산에 {project} 사업비 2억 원을 반영하겠습니다."
    )

    result = await HeuristicProvider().extract(_context(text, ["정보화과장 김서준"]))

    assert len(result.value.signals) == 1
    signal = result.value.signals[0]
    assert project in signal.title
    assert signal.commitment == "committed"
    assert signal.budget_krw == 200_000_000
    assert signal.evidence == [f"내년도 본예산에 {project} 사업비 2억 원을 반영하겠습니다."]
