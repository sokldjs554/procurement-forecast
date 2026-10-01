"""Invented money quotes must keep their own amount through production grounding."""

from datetime import date

import pytest

from app.domain.grounding import locate_quote
from app.llm.schemas import ExtractedSignal
from app.pipeline.process import CheckedSignal, check_signal


def check_budget(
    statements: list[str],
    claim: int,
    quote: str | None,
    *,
    evidence: list[str] | None = None,
    doc_type: str = "council_minutes",
) -> CheckedSignal:
    source = "○정보화과장 김서준  " + " ".join(statements)
    signal = ExtractedSignal(
        title="데이터 구매",
        summary="가상시 데이터 구매 예산",
        category="ai_data",
        institution_mention="가상시",
        department=None,
        budget_text=quote,
        budget_krw=claim,
        timing_text=None,
        expected_year=None,
        expected_half=None,
        commitment="planned",
        procurement_type="goods",
        keywords=["데이터"],
        evidence=statements if evidence is None else evidence,
        confidence=0.9,
    )
    return check_signal(
        signal,
        text=source,
        doc_type=doc_type,
        reference_date=date(2026, 10, 1),
        fiscal_year=None,
        table_unit=1000,
        min_score=88.0,
    )


def assert_unsupported(checked: CheckedSignal, claim: int) -> None:
    assert checked.budget_krw is None
    assert checked.report.budget_claimed == claim
    assert checked.report.budget_grounded is False
    assert checked.report.budget_parsed is None
    assert checked.report.verdict == "needs_review"


def test_absent_money_quote_cannot_borrow_historical_amount() -> None:
    checked = check_budget(
        [
            "올해까지 분석 용역비로 8,000만 원을 사용했습니다.",
            "내년에는 데이터를 직접 구매할 예정이며 구매비를 2,000 정도 잡았습니다.",
        ],
        20_000_000,
        "2,000만원",
    )
    assert_unsupported(checked, 20_000_000)


def test_fuzzy_money_quote_cannot_change_digits_even_within_numeric_tolerance() -> None:
    statement = "데이터 구매 예산으로 123,456,780원을 편성할 계획입니다."
    quote = "123,456,789원"
    assert locate_quote(statement, quote).method == "fuzzy"
    assert_unsupported(check_budget([statement], 123_456_789, quote), 123_456_789)


@pytest.mark.parametrize("claim", [100_000_000, 200_000_000])
def test_no_money_quote_and_multiple_exact_amounts_are_ambiguous(claim: int) -> None:
    checked = check_budget(
        ["데이터 구매에 2억 원을 편성할 계획입니다.", "서버 교체에 5억 원을 편성할 계획입니다."],
        claim,
        None,
    )
    assert_unsupported(checked, claim)


def test_multiple_amounts_in_explicit_quote_never_choose_largest_correction() -> None:
    quote = "총 5억 원 중 데이터 구매에 2억 원"
    checked = check_budget([quote + "을 편성할 계획입니다."], 100_000_000, quote)
    assert_unsupported(checked, 100_000_000)


@pytest.mark.parametrize("quote", ["2억 원", None])
def test_single_literal_amount_still_corrects_wrong_conversion(quote: str | None) -> None:
    checked = check_budget(["데이터 구매에 2억 원을 편성할 계획입니다."], 20_000_000, quote)
    assert checked.budget_krw == 200_000_000
    assert checked.report.budget_claimed == 20_000_000
    assert checked.report.budget_grounded is False
    assert checked.report.budget_parsed == 200_000_000
    assert "budget_mismatch" in checked.report.issues


def test_explicit_money_quote_keeps_its_correct_amount_among_other_projects() -> None:
    checked = check_budget(
        ["데이터 구매에 2억 원을 편성할 계획입니다.", "서버 교체에 5억 원을 편성할 계획입니다."],
        200_000_000,
        "2억 원",
    )
    assert checked.budget_krw == checked.report.budget_parsed == 200_000_000
    assert checked.report.budget_grounded is True
    assert checked.report.verdict == "accepted"


@pytest.mark.parametrize("quote", ["2억원", "2억"])
def test_short_literal_money_quotes_remain_grounded(quote: str) -> None:
    checked = check_budget([f"데이터 구매에 {quote}을 편성할 계획입니다."], 200_000_000, quote)
    assert checked.budget_krw == 200_000_000
    assert checked.report.budget_grounded is True


@pytest.mark.parametrize(("source_amount", "quote"), [("12억원", "2억원"), ("2억5천만원", "2억")])
def test_short_money_quote_cannot_select_part_of_a_larger_amount(
    source_amount: str, quote: str
) -> None:
    checked = check_budget(
        [f"데이터 구매에 {source_amount}을 편성할 계획입니다."], 200_000_000, quote
    )
    assert_unsupported(checked, 200_000_000)


def test_unquoted_unsupported_claim_is_cleared_but_original_claim_remains() -> None:
    checked = check_budget(["데이터 구매 예산은 나중에 결정할 계획입니다."], 200_000_000, None)
    assert_unsupported(checked, 200_000_000)


def test_fuzzy_evidence_does_not_supply_an_unquoted_amount() -> None:
    source = "데이터 구매에 123,456,780원을 편성할 계획입니다."
    evidence = source.replace("123,456,780", "123,456,789")
    assert locate_quote(source, evidence).method == "fuzzy"
    assert_unsupported(check_budget([source], 123_456_789, None, evidence=[evidence]), 123_456_789)


def test_exact_quote_without_money_does_not_borrow_another_quote_amount() -> None:
    checked = check_budget(
        ["데이터 구매 예산은 추후 결정할 계획입니다.", "서버 교체에 5억 원을 편성할 계획입니다."],
        200_000_000,
        "추후 결정",
    )
    assert_unsupported(checked, 200_000_000)


def test_literal_bare_cell_in_explicit_won_table_remains_grounded() -> None:
    checked = check_budget(
        ["(단위: 원)\n데이터 구매 12,345,000 0 12,345,000"],
        12_345_000,
        "12,345,000",
        doc_type="budget_book",
    )
    assert checked.budget_krw == 12_345_000
    assert checked.report.budget_grounded is True
