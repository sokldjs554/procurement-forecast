"""Unmarked, standalone speaker headings retain source attribution and offsets."""

from datetime import date

import pytest

from app.domain.grounding import locate_quote, official_evidence_issue
from app.llm.prompts import ChunkContext
from app.llm.providers.heuristic import HeuristicProvider
from app.parsing.chunking import chunk_minutes, split_turns
from app.pipeline.process import check_signal
from app.pipeline.triage import triage_chunk

PROJECT = "내년도 본예산에 정보시스템 구축 사업비 4억 원을 반영하겠습니다."


async def _extract(text: str, labels: list[str] | None = None):
    ctx = ChunkContext(
        "council_minutes", "합성 회의", "가상시", date(2026, 9, 1), labels or [], text
    )
    return (await HeuristicProvider().extract(ctx)).value.signals


@pytest.mark.parametrize("member", ["위원 이민우", "이민우 위원", "이민우 의원"])
async def test_plain_exchange_passes_production_gates_with_exact_source_offsets(
    member: str,
) -> None:
    text = f"회의록 머리말\n{member}\n내년도 사업은 무엇입니까?\n정보화과장 김서준\n{PROJECT}"
    chunks = chunk_minutes(text)
    chunk = next(chunk for chunk in chunks if chunk.kind == "exchange")
    assert chunk.kind == "exchange"
    assert text[chunk.char_start : chunk.char_end] == chunk.text
    assert triage_chunk(chunk.text, kind=chunk.kind, threshold=0.35).passed
    signals = await _extract(chunk.text, chunk.labels)
    assert len(signals) == 1
    signal = signals[0]
    assert signal.budget_krw == 400_000_000
    assert signal.expected_year == 2027
    assert signal.evidence == [PROJECT]
    checked = check_signal(
        signal,
        text=chunk.text,
        doc_type="council_minutes",
        reference_date=date(2026, 9, 1),
        fiscal_year=None,
        table_unit=1,
        min_score=88.0,
        document_text=text,
        char_start=chunk.char_start,
    )
    assert checked.report.verdict == "accepted"


@pytest.mark.parametrize(
    "heading",
    [
        "위원 이민우",
        "이민우 위원",
        "위원장 이민우",
        "전문위원 이민우",
        "참고인 이민우",
        "증인 이민우",
        "공술인 이민우",
        "주민 이민우",
        "이민우 위원장",
        "이민우 의장",
    ],
)
async def test_plain_nonexecutive_stops_previous_official_authority(heading: str) -> None:
    text = f"정보화과장 김서준\n설명을 마치겠습니다.\n{heading}\n{PROJECT}"
    assert len(split_turns(text)) == 2
    assert await _extract(text, ["정보화과장 김서준"]) == []
    assert (
        official_evidence_issue(
            text, [locate_quote(text, PROJECT)], char_start=0, commitment="committed"
        )
        == "official_evidence_missing"
    )


async def test_inline_plain_prose_is_not_a_speaker_heading() -> None:
    text = "정보화과장 김서준 " + PROJECT
    assert split_turns(text) == []
    assert await _extract(text) == []


async def test_plain_heading_prevents_label_from_attributing_preamble() -> None:
    assert await _extract(PROJECT + "\n이민우 위원\n회의를 마칩니다.", ["정보화과장 김서준"]) == []


async def test_plain_official_continuation_keeps_attribution() -> None:
    text = "정보화과장 김서준\n" + "지금까지의 경과를 설명드립니다. " * 180 + PROJECT
    chunk = chunk_minutes(text)[-1]
    assert chunk.char_start > 0
    signals = await _extract(chunk.text, chunk.labels)
    assert len(signals) == 1
    assert (
        official_evidence_issue(
            text,
            [locate_quote(chunk.text, PROJECT)],
            char_start=chunk.char_start,
            commitment="committed",
        )
        is None
    )


def test_plain_header_does_not_pad_short_assent_evidence() -> None:
    text = "디지털정보화과장 김서준\n네, 맞습니다."
    assert (
        official_evidence_issue(
            text, [locate_quote(text, text)], char_start=0, commitment="committed"
        )
        == "official_evidence_ambiguous"
    )


async def test_unrecognized_marked_speaker_stops_official_continuation() -> None:
    text = "설명을 마칩니다.\n○미상\n" + PROJECT
    assert await _extract(text, ["정보화과장 김서준"]) == []


async def test_audio_placeholder_keeps_review_candidate_without_executive_authority() -> None:
    text = "○발언자 미상 " + PROJECT
    signals = await _extract(text)
    assert len(signals) == 1
    checked = check_signal(
        signals[0],
        text=text,
        doc_type="council_minutes",
        reference_date=date(2026, 9, 1),
        fiscal_year=None,
        table_unit=1,
        min_score=88.0,
    )
    assert checked.report.verdict == "needs_review"
    assert "official_evidence_missing" in checked.report.issues
