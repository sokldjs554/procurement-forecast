import pytest

from app.domain.krw import (
    amounts_agree,
    detect_table_unit,
    find_amounts,
    format_krw,
    parse_krw,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3억 5천만원", 350_000_000),
        ("3억5,000만 원", 350_000_000),
        ("350,000천원", 350_000_000),
        ("350백만원", 350_000_000),
        ("1.2억", 120_000_000),
        ("12억원", 1_200_000_000),
        ("5천만원", 50_000_000),
        ("삼억 오천만 원", 350_000_000),
        ("일천만원", 10_000_000),
        ("금 350,000,000원", 350_000_000),
        ("사업비는 약 2억 원 규모로", 200_000_000),
        ("총 1조 2천억원", 1_200_000_000_000),
        # spoken: the unit after the tail is left out (found by `manage eval llm`, case r14)
        ("내년도에 2억 8천 정도 들 것으로", 280_000_000),
        ("1억 5백 규모", 105_000_000),
        ("1조 2천", 1_200_000_000_000),
        # the same with a bare number (성남시의회 minutes: "예산액은 한 2억 4000 정도로")
        ("예산액은 한 2억 4000 정도로 예산을 하고", 240_000_000),
        ("1조 5000 규모", 1_500_000_000_000),
        ("5만 3천원", 53_000),  # after 만 the tail is literal
        ("3억 5천만원을 편성", 350_000_000),  # explicit 만 unchanged
        # written amounts (ending in 원) are exact, however the tail is spelled
        ("금 일억이천삼백원정", 100_002_300),
        ("2억 3천원", 200_003_000),
        ("2억 8천 5백원", 200_008_500),
        ("2억 8천500원", 200_008_500),
        ("2억 4000원", 200_004_000),
    ],
)
def test_parse_krw_spoken_and_written_forms(text: str, expected: int) -> None:
    assert parse_krw(text) == expected


def test_trailing_year_is_not_swallowed_into_amount() -> None:
    assert parse_krw("사업비 2억 2027년 착공 예정") == 200_000_000


def test_bare_tail_is_not_a_count_year_or_decimal() -> None:
    assert parse_krw("2억 10개소 설치") == 200_000_000
    assert parse_krw("2억 2027년도 착공") == 200_000_000
    assert parse_krw("2억 4000.5") == 200_000_000


def test_hangul_digits_do_not_fire_on_place_names() -> None:
    assert find_amounts("이천시와 일부 구간") == []


def test_table_cell_uses_default_unit() -> None:
    assert parse_krw("350,000", default_unit=1000) == 350_000_000


def test_find_amounts_reports_offsets() -> None:
    text = "스마트쉘터 7개소 설치에 3억 5천만원, 유지보수 2천만원"
    found = find_amounts(text)
    assert [a.value for a in found] == [350_000_000, 20_000_000]
    first = found[0]
    assert text[first.start : first.end].startswith("3억")


@pytest.mark.parametrize(
    ("header", "unit"),
    [("(단위: 천원)", 1000), ("단위 : 백만원", 1_000_000), ("(단위：원)", 1)],
)
def test_detect_table_unit(header: str, unit: int) -> None:
    assert detect_table_unit(f"2027년도 세출예산 사업명세서 {header}") == unit


def test_amounts_agree_tolerance() -> None:
    assert amounts_agree(350_000_000, 351_000_000)
    assert not amounts_agree(350_000_000, 300_000_000)
    assert not amounts_agree(0, 5)


@pytest.mark.parametrize(
    ("value", "text"),
    [(350_000_000, "3억 5,000만원"), (50_000_000, "5,000만원"), (1_234, "1,234원")],
)
def test_format_krw(value: int, text: str) -> None:
    assert format_krw(value) == text
