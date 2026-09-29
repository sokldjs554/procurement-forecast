"""Persisted evidence must stay attached to the exact original source speaker."""

from datetime import date

import pytest

from app.db.models import Document, DocumentChunk, Signal
from app.pipeline.revalidate import validate_stored_signal

DAY = date(2026, 9, 29)
QUOTE = "스마트쉘터를 설치할 계획입니다."


def stored(
    text: str, quote: str = QUOTE, *, at: int | None = None, doc_type: str = "council_minutes"
) -> tuple[Signal, Document, DocumentChunk]:
    start = text.index(quote) if at is None else at
    document = Document(id=1, doc_type=doc_type, text=text, parse_status="parsed", structured={})
    chunk = DocumentChunk(id=1, document_id=1, char_start=0, char_end=len(text), text=text)
    signal = Signal(
        id=1,
        document_id=1,
        chunk_id=1,
        stage="council_mention" if doc_type == "council_minutes" else "budget_line",
        title="스마트쉘터",
        institution_code="LG-41130",
        budget_krw=None,
        expected_year=None,
        confidence=0.95,
        observed_at=DAY,
        evidence=[{"quote": quote, "start": start, "end": start + len(quote)}],
        grounding={},
    )
    return signal, document, chunk


def test_same_quote_earlier_official_cannot_validate_later_member() -> None:
    source = f"○교통과장 이민수  {QUOTE}\n○위원 김민수  {QUOTE}"
    signal, document, chunk = stored(source, at=source.rindex(QUOTE))
    report = validate_stored_signal(signal, document, chunk)
    assert report.verdict == "needs_review"
    assert "official_evidence_missing" in report.issues
    assert report.evidence[0].start == source.rindex(QUOTE)


def test_source_full_text_proves_official_in_continuation_chunk() -> None:
    source = f"○교통과장 이민수  앞선 설명입니다.\n{QUOTE}"
    signal, document, chunk = stored(source)
    chunk.char_start = source.index(QUOTE)
    chunk.text = source[chunk.char_start :]
    report = validate_stored_signal(signal, document, chunk)
    assert report.verdict == "accepted"


@pytest.mark.parametrize("mutation", ["no_text", "stale_offsets", "no_offsets", "chunk_changed"])
def test_missing_or_stale_provenance_is_never_accepted(mutation: str) -> None:
    signal, document, chunk = stored(f"○교통과장 이민수  {QUOTE}")
    if mutation == "no_text":
        document.text = None
    elif mutation == "stale_offsets":
        signal.evidence[0]["start"] = 0
        signal.evidence[0]["end"] = 10
    elif mutation == "no_offsets":
        signal.evidence[0].pop("start")
    else:
        chunk.text = "다른 문서 내용"
    assert validate_stored_signal(signal, document, chunk).verdict != "accepted"


def test_budget_and_timing_are_recovered_from_source_not_old_grounding_flags() -> None:
    quote = "2027년에 스마트쉘터를 3억 원으로 설치할 계획입니다."
    signal, document, chunk = stored(f"○교통과장 이민수  {quote}", quote)
    signal.budget_krw = 900_000_000
    signal.expected_year = 2028
    signal.grounding = {"budget_grounded": True, "year_grounded": True}
    report = validate_stored_signal(signal, document, chunk)
    assert report.verdict == "needs_review"
    assert {"budget_mismatch", "year_unverified"} <= set(report.issues)
    assert signal.budget_krw == 900_000_000  # audit never silently repairs a financial claim


def test_budget_book_table_units_and_fiscal_year_are_respected() -> None:
    quote = "스마트쉘터 설치 352,000"
    signal, document, chunk = stored(f"(단위:천원)\n{quote}", quote, doc_type="budget_book")
    signal.budget_krw = 352_000_000
    signal.expected_year = 2027
    document.structured = {"fiscal_year": 2027}
    assert validate_stored_signal(signal, document, chunk).verdict == "accepted"
    signal.expected_year = 2028
    assert "fiscal_year_mismatch" in validate_stored_signal(signal, document, chunk).issues


def test_grounding_report_offsets_cannot_stand_in_for_document_offsets() -> None:
    signal, document, chunk = stored(f"○교통과장 이민수  {QUOTE}")
    signal.grounding = {"evidence": signal.evidence}
    signal.evidence = []
    report = validate_stored_signal(signal, document, chunk)
    assert "global_evidence_offsets_missing" in report.issues
    assert report.verdict == "needs_review"


@pytest.mark.parametrize(
    ("source_quote", "stored_quote", "commitment", "issue"),
    [
        (
            "스마트쉘터 설치 사업을 추진할 계획은 없습니다.",
            "스마트쉘터 설치 사업을 추진할 계획은 없습니다.",
            "planned",
            "commitment_denied",
        ),
        (
            "사업비가 확보되면 스마트쉘터를 설치할 계획입니다.",
            "스마트쉘터를 설치할 계획입니다.",
            "committed",
            "commitment_conditional",
        ),
    ],
)
def test_current_source_semantics_downgrade_old_accepted_commitments(
    source_quote: str, stored_quote: str, commitment: str, issue: str
) -> None:
    signal, document, chunk = stored(f"○교통과장 이민수  {source_quote}", stored_quote)
    signal.commitment = commitment
    report = validate_stored_signal(signal, document, chunk)
    assert report.verdict == "needs_review"
    assert issue in report.issues
