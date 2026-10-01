"""Experimental local extraction always needs review, including after revalidation."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest

from app.db.models import Document, DocumentChunk, Signal
from app.llm.schemas import ExtractedSignal
from app.parsing.chunking import Chunk
from app.pipeline.process import _text_signal
from app.pipeline.revalidate import validate_stored_signal
from app.runtime import Runtime

LOCAL = "local_llama:local-qwen3-4b-q4_k_m-7485fe6f:extract-v3"
EXTERNAL = "anthropic:claude-opus-5:extract-v3"
QUOTE = "데이터 구매에 2억 원을 편성하겠습니다."


def text_record(extractor: str, *, rejected: bool = False) -> tuple[dict, Document]:
    text = "○정보화과장 김서준  " + QUOTE
    document = Document(
        id=1,
        doc_type="council_minutes",
        text=text,
        parse_status="parsed",
        structured={},
        institution_code="INVENTED-CITY",
        published_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    runtime = cast(
        Runtime,
        SimpleNamespace(
            settings=SimpleNamespace(grounding_min_score=88.0),
            registry=SimpleNamespace(get=lambda _: None),
        ),
    )
    signal = ExtractedSignal(
        title="데이터 구매",
        summary="가상시 데이터 구매",
        category="ai_data",
        institution_mention=None,
        department=None,
        budget_text="2억 원",
        budget_krw=200_000_000,
        timing_text=None,
        expected_year=None,
        expected_half=None,
        commitment="committed",
        procurement_type="goods",
        keywords=["데이터"],
        confidence=0.9,
        evidence=["원문에 존재하지 않는 전혀 다른 사업의 증거 문장입니다." if rejected else QUOTE],
    )
    row = _text_signal(
        runtime,
        document,
        Chunk(0, 0, len(text), text),
        signal,
        extractor=extractor,
        degraded_reason=None,
        fiscal_year=None,
        table_unit=1000,
    )
    return row, document


@pytest.mark.parametrize(
    ("extractor", "verdict"), [(LOCAL, "needs_review"), (EXTERNAL, "accepted")]
)
def test_real_text_signal_applies_local_review_policy(extractor: str, verdict: str) -> None:
    row, _ = text_record(extractor)
    assert row["verdict"] == verdict
    assert row["grounding"]["verdict"] == verdict
    assert ("local_model_requires_review" in row["grounding"]["issues"]) == (extractor == LOCAL)


def test_local_review_policy_never_promotes_rejected_evidence() -> None:
    row, _ = text_record(LOCAL, rejected=True)
    assert row["verdict"] == "rejected"
    assert "local_model_requires_review" in row["grounding"]["issues"]


@pytest.mark.parametrize(
    ("extractor", "verdict"), [(LOCAL, "needs_review"), (EXTERNAL, "accepted")]
)
def test_stored_revalidation_recovers_policy_from_extractor(extractor: str, verdict: str) -> None:
    row, document = text_record(extractor)
    signal = Signal(id=1, document_id=1, chunk_id=1, **row)
    # Even a legacy row without the saved marker cannot become auto-approved.
    signal.grounding = {}
    signal.verdict = "accepted"
    chunk = DocumentChunk(
        id=1, document_id=1, char_start=0, char_end=len(document.text), text=document.text
    )
    report = validate_stored_signal(signal, document, chunk)
    assert report.verdict == verdict
    assert ("local_model_requires_review" in report.issues) == (extractor == LOCAL)
