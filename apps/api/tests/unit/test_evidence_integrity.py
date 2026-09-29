"""An exact quote is not enough: its speaker must support the claimed signal."""

from datetime import date

import pytest

from app.llm.schemas import ExtractedSignal
from app.pipeline.process import check_signal


def checked(text, quotes, **kwargs):  # type: ignore[no-untyped-def]
    sig = ExtractedSignal.model_validate(
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
            "commitment": "planned",
            "procurement_type": "goods",
            "keywords": [],
            "evidence": quotes,
            "confidence": 0.95,
        }
    )
    return check_signal(
        sig,
        text=text,
        doc_type="council_minutes",
        reference_date=date(2026, 1, 1),
        fiscal_year=None,
        table_unit=1,
        min_score=88,
        **kwargs,
    ).report


@pytest.mark.parametrize(
    "speaker",
    [
        "위원 김민수",
        "김민수위원",
        "김민수 의원",
        "위원장 김민수",
        "부위원장 김민수",
        "행정복지위원장 김민수",
    ],
)
def test_member_quote_cannot_be_an_executive_plan(speaker: str) -> None:
    text = (
        f"○{speaker}  스마트쉘터를 설치해 주십시오.\n○교통과장 이민수  현재 추진 계획은 없습니다."
    )
    report = checked(text, ["스마트쉘터를 설치해 주십시오."])
    assert report.verdict == "needs_review"
    assert "official_evidence_missing" in report.issues


def test_subject_in_question_and_substantive_official_answer_are_allowed() -> None:
    text = "○위원 김민수  스마트쉘터를 설치해 주십시오.\n○교통과장 이민수  해당 사업은 내년부터 추진할 예정입니다."
    assert (
        checked(
            text, ["스마트쉘터를 설치해 주십시오.", "해당 사업은 내년부터 추진할 예정입니다."]
        ).verdict
        == "accepted"
    )


def test_bare_acknowledgement_does_not_validate_the_members_plan() -> None:
    text = "○위원 김민수  스마트쉘터를 설치해 주십시오.\n○교통과장 이민수  네, 맞습니다."
    report = checked(text, ["스마트쉘터를 설치해 주십시오.", "네, 맞습니다."])
    assert report.verdict == "needs_review"
    assert "official_evidence_ambiguous" in report.issues


def test_unknown_speaker_is_not_assumed_to_be_an_official() -> None:
    report = checked("스마트쉘터를 설치할 계획입니다.", ["스마트쉘터를 설치할 계획입니다."])
    assert report.verdict == "needs_review"


def test_speaker_is_retained_for_a_continuation_chunk() -> None:
    full = "○교통과장 이민수  앞선 설명입니다.\n스마트쉘터를 설치할 계획입니다."
    start = full.index("스마트쉘터")
    report = checked(
        full[start:], ["스마트쉘터를 설치할 계획입니다."], document_text=full, char_start=start
    )
    assert report.verdict == "accepted"


def test_quote_crossing_a_speaker_boundary_is_not_an_official_quote() -> None:
    text = "○위원 김민수  스마트쉘터를 설치해 주십시오.\n○교통과장 이민수  네."
    report = checked(text, [text[text.index("스마트쉘터") :]])
    assert report.verdict == "needs_review"


def test_unquoted_timing_is_not_treated_as_grounded() -> None:
    from app.domain.grounding import verify_extraction

    report = verify_extraction(
        source="스마트쉘터를 설치할 계획입니다.",
        evidence_quotes=["스마트쉘터를 설치할 계획입니다."],
        budget_krw=None,
        budget_text=None,
        expected_year=2027,
        timing_text="2027년",
        reference_date=date(2026, 1, 1),
        confidence=0.95,
    )
    assert report.verdict == "needs_review"
    assert "year_unverified" in report.issues


def test_fuzzy_timing_reads_the_actual_source_year():
    from app.domain.grounding import verify_extraction

    report = verify_extraction(
        source="2026년 하반기에 설치할 계획입니다.",
        evidence_quotes=["2026년 하반기에 설치할 계획입니다."],
        budget_krw=None,
        budget_text=None,
        expected_year=2027,
        timing_text="2027년 하반기에 설치할 계획입니다.",
        reference_date=date(2026, 1, 1),
        confidence=0.95,
    )
    assert report.verdict == "needs_review"
    assert report.year_resolved == 2026


def test_fuzzy_budget_reads_the_actual_source_amount():
    from app.domain.grounding import verify_extraction

    report = verify_extraction(
        source="스마트쉘터 설치 예산은 3억 원입니다.",
        evidence_quotes=["스마트쉘터 설치 예산은 3억 원입니다."],
        budget_krw=400_000_000,
        budget_text="스마트쉘터 설치 예산은 4억 원입니다.",
        expected_year=None,
        timing_text=None,
        reference_date=date(2026, 1, 1),
        confidence=0.95,
    )
    assert report.verdict == "needs_review"
    assert report.budget_parsed == 300_000_000
