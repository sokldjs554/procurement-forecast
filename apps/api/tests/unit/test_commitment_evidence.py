"""Council evidence must support the strength of the extracted commitment."""

from datetime import date

import pytest

from app.domain.grounding import GroundingReport
from app.llm.schemas import Commitment, ExtractedSignal
from app.pipeline.process import check_signal


def _checked(
    text: str,
    commitment: Commitment,
    quotes: list[str] | None = None,
    *,
    document_text: str | None = None,
    char_start: int = 0,
    doc_type: str = "council_minutes",
) -> GroundingReport:
    signal = ExtractedSignal.model_validate(
        {
            "title": "스마트쉘터 설치",
            "summary": "설치 계획",
            "category": "smart_city",
            "institution_mention": None,
            "department": None,
            "budget_text": None,
            "budget_krw": None,
            "timing_text": None,
            "expected_year": None,
            "expected_half": None,
            "commitment": commitment,
            "procurement_type": "goods",
            "keywords": [],
            "evidence": quotes if quotes is not None else [text],
            "confidence": 0.95,
        }
    )
    return check_signal(
        signal,
        text=text,
        doc_type=doc_type,
        reference_date=date(2026, 1, 1),
        fiscal_year=None,
        table_unit=1,
        min_score=88,
        document_text=document_text,
        char_start=char_start,
    ).report


@pytest.mark.parametrize("commitment", ["planned", "committed"])
@pytest.mark.parametrize(
    "answer",
    [
        "스마트쉘터 설치 사업을 추진할 계획은 없습니다.",
        "스마트쉘터에 대한 현재 추진 계획은 없습니다.",
        "스마트쉘터 설치 사업은 검토하지 않고 있습니다.",
        "스마트쉘터 설치 사업비를 내년 예산에 반영하지 않겠습니다.",
    ],
)
def test_current_denial_cannot_support_a_positive_commitment(
    answer: str, commitment: Commitment
) -> None:
    report = _checked(f"○교통과장 이민수  {answer}", commitment, [answer])
    assert report.verdict == "needs_review"
    assert "commitment_denied" in report.issues


@pytest.mark.parametrize(
    "answer",
    [
        "스마트쉘터 사업비가 확보되면 추진하겠습니다.",
        "스마트쉘터 설치 사업이 승인되면 추진하겠습니다.",
        "스마트쉘터 사업의 타당성 검토 후 추진하겠습니다.",
        "스마트쉘터 설치가 가능하다면 추진하겠습니다.",
    ],
)
def test_conditional_intent_cannot_support_a_committed_signal(answer: str) -> None:
    report = _checked(f"○교통과장 이민수  {answer}", "committed", [answer])
    assert report.verdict == "needs_review"
    assert "commitment_conditional" in report.issues


def test_omitted_condition_is_read_from_the_same_source_sentence() -> None:
    text = "○교통과장 이민수  사업비가 확보되면 스마트쉘터 설치 사업을 추진하겠습니다."
    report = _checked(text, "committed", ["스마트쉘터 설치 사업을 추진하겠습니다."])
    assert report.verdict == "needs_review"
    assert "commitment_conditional" in report.issues


def test_condition_before_a_continuation_chunk_is_not_lost() -> None:
    full = "○교통과장 이민수  사업비가 확보되면\n스마트쉘터 설치 사업을 추진하겠습니다."
    start = full.index("스마트쉘터")
    report = _checked(full[start:], "committed", document_text=full, char_start=start)
    assert report.verdict == "needs_review"
    assert "commitment_conditional" in report.issues


@pytest.mark.parametrize(
    ("answer", "commitment"),
    [
        ("스마트쉘터 설치 사업을 추진할 계획은 없습니다.", "declined"),
        ("스마트쉘터 설치 사업은 검토하지 않고 있습니다.", "declined"),
        ("스마트쉘터 설치 사업의 타당성 검토 후 추진하겠습니다.", "reviewing"),
        ("스마트쉘터 사업비가 확보되면 추진할 계획입니다.", "planned"),
        ("스마트쉘터 설치에는 문제없습니다. 올해 추진하겠습니다.", "committed"),
        ("스마트쉘터 설치 사업을 차질 없이 추진하겠습니다.", "committed"),
        ("스마트쉘터 사업 타당성 검토를 마치고 추진하겠습니다.", "committed"),
    ],
)
def test_accurate_or_unconditional_commitments_remain_accepted(
    answer: str, commitment: Commitment
) -> None:
    report = _checked(f"○교통과장 이민수  {answer}", commitment, [answer])
    assert report.verdict == "accepted", report.issues


@pytest.mark.parametrize(
    ("answer", "quote"),
    [
        (
            '위원님께서 "스마트쉘터 설치 사업을 추진하겠습니다"라고 말씀하셨습니다.',
            "스마트쉘터 설치 사업을 추진하겠습니다",
        ),
        (
            "위원님께서 ‘스마트쉘터 설치 사업을 추진하겠습니다’라고 말씀하셨습니다.",
            "스마트쉘터 설치 사업을 추진하겠습니다",
        ),
        (
            "위원님께서 스마트쉘터 설치 사업을 추진하겠다는 요구를 전달하셨습니다.",
            "스마트쉘터 설치 사업을 추진하겠다는 요구를 전달하셨습니다.",
        ),
        (
            "스마트쉘터 설치 사업을 추진할 계획이 있습니까?",
            "스마트쉘터 설치 사업을 추진할 계획이 있습니까?",
        ),
    ],
)
def test_reported_speech_or_official_question_is_not_direct_support(
    answer: str, quote: str
) -> None:
    report = _checked(f"○교통과장 이민수  {answer}", "committed", [quote])
    assert report.verdict == "needs_review"
    assert "commitment_context_ambiguous" in report.issues


def test_members_condition_does_not_override_an_unconditional_official_answer() -> None:
    question = "스마트쉘터 설치 사업비가 확보되면 추진할 수 있습니까?"
    answer = "스마트쉘터 설치 예산을 확보하였고 올해 추진하겠습니다."
    text = f"○위원 김민수  {question}\n○교통과장 이민수  {answer}"
    report = _checked(text, "committed", [question, answer])
    assert report.verdict == "accepted", report.issues


def test_quoted_denial_does_not_override_a_direct_official_commitment() -> None:
    answer = (
        "위원님께서 ‘현재 추진 계획은 없습니다’라는 표현을 지적하셨습니다. "
        "스마트쉘터 설치 사업은 올해 추진하겠습니다."
    )
    report = _checked(f"○교통과장 이민수  {answer}", "committed", [answer])
    assert report.verdict == "accepted", report.issues


def test_unquoted_unrelated_denial_in_another_sentence_does_not_override_evidence() -> None:
    answer = "도로 확장 사업은 현재 추진 계획은 없습니다. 스마트쉘터를 설치할 계획입니다."
    report = _checked(f"○교통과장 이민수  {answer}", "planned", ["스마트쉘터를 설치할 계획입니다."])
    assert report.verdict == "accepted", report.issues


def test_guard_does_not_apply_council_semantics_to_a_budget_book() -> None:
    text = "스마트쉘터 검토 후 추진 사업 예산 300,000"
    report = _checked(text, "committed", doc_type="budget_book")
    assert report.verdict == "accepted", report.issues


def test_reported_commitment_plus_bare_acknowledgement_still_requires_review() -> None:
    report_quote = "위원님께서 ‘스마트쉘터를 설치하겠습니다’라고 말씀하셨습니다."
    text = f"○교통과장 이민수  {report_quote} 네, 맞습니다."
    report = _checked(text, "committed", [report_quote, "네, 맞습니다."])
    assert report.verdict == "needs_review"
    assert "commitment_context_ambiguous" in report.issues


def test_completed_action_after_review_is_not_still_conditional() -> None:
    answer = "스마트쉘터 설치 사업은 타당성 검토 후 예산에 반영하였습니다."
    report = _checked(f"○교통과장 이민수  {answer}", "committed", [answer])
    assert report.verdict == "accepted", report.issues


def test_fuzzy_quote_cannot_change_the_actual_official_denial() -> None:
    answer = "스마트쉘터 설치 사업을 추진할 계획은 없습니다."
    report = _checked(
        f"○교통과장 이민수  {answer}",
        "planned",
        ["스마트쉘터 설치 사업을 추진할 계획은 있습니다."],
    )
    assert report.evidence[0].method == "fuzzy"
    assert report.verdict == "needs_review"
    assert "commitment_denied" in report.issues
