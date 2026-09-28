"""Korean-won amount parsing.

Public documents express the same number in many ways, and the parser has to agree with itself
on all of them because it is the referee that checks the LLM's ``budget_krw`` against the quote:

    "3억 5천만원"      → 350,000,000   (spoken form, council minutes)
    "3억5,000만 원"    → 350,000,000
    "350,000천원"      → 350,000,000   (budget books are denominated in 천원)
    "350백만원"         → 350,000,000   (procurement plans sometimes use 백만원)
    "1.2억"            → 120,000,000
    "삼억 오천만 원"    → 350,000,000   (hangul numerals — appear in transcribed speech)
    "350,000" + unit=1000 (table cell under "(단위: 천원)") → 350,000,000
    "2억 8천 정도"     → 280,000,000   (spoken: the 만 after 8천 is left out)
    "2억 4000 정도"    → 240,000,000   (spoken: the 만 after 4000 is left out)

The algorithm is the usual positional one: small units (십/백/천) accumulate into a section,
large units (만/억/조) flush the section into the total. One spoken convention on top: a trailing
section made of small units right after 억 or 조 counts in the next unit down (억 → 만, 조 → 억),
because nobody says "2억 8천" meaning 200,008,000. Amounts ending in 원 are written, not spoken,
and stay exact ("금 일억이천삼백원" → 100,002,300).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_SMALL = {"십": 10, "백": 100, "천": 1000}
_LARGE = {"만": 10**4, "억": 10**8, "조": 10**12}
_HANGUL_DIGITS = {
    "영": 0,
    "공": 0,
    "일": 1,
    "이": 2,
    "삼": 3,
    "사": 4,
    "오": 5,
    "육": 6,
    "륙": 6,
    "칠": 7,
    "팔": 8,
    "구": 9,
}

# An amount span: numbers (arabic with , . or hangul digits) interleaved with units, optionally
# followed by 원. Whitespace between parts is allowed ("3억 5천만 원").
_NUM = r"\d[\d,]*(?:\.\d+)?"
_HNUM = r"[영공일이삼사오육륙칠팔구]"
_UNIT = r"[십백천만억조]"
_PART = rf"(?:{_NUM}|{_HNUM})\s?{_UNIT}+|{_UNIT}+"
# Spoken tail with no unit right after 억/조 ("2억 4000 정도"): up to four digits, not a year,
# date or count ("2억 2027년", "3억 10개소").
_BARE_TAIL = r"(?<=[억조])\s?\d{1,4}(?![\d,.]|\s?[년월일개명건호차회층곳%])"
_SPAN_RE = re.compile(
    rf"(?P<body>(?:{_NUM}|{_HNUM})\s?{_UNIT}+(?:\s?(?:{_PART}))*"
    rf"(?:\s?{_NUM}(?=\s?원)|{_BARE_TAIL})?)\s?(?P<won>원)?"
    rf"|(?P<plain>{_NUM})\s?(?P<won2>원)"
)
_TOKEN_RE = re.compile(rf"{_NUM}|{_HNUM}|{_UNIT}")


@dataclass(frozen=True, slots=True)
class AmountMatch:
    value: int
    start: int
    end: int
    raw: str
    has_won_suffix: bool


def _to_decimal(token: str) -> Decimal | None:
    if token in _HANGUL_DIGITS:
        return Decimal(_HANGUL_DIGITS[token])
    try:
        return Decimal(token.replace(",", ""))
    except InvalidOperation:
        return None


def _evaluate(body: str, *, spoken: bool = True) -> int | None:
    """``spoken=False`` for spans ending in 원: written amounts ("금 일억이천삼백원") are exact."""
    total = Decimal(0)
    section = Decimal(0)
    pending: Decimal | None = None
    saw_unit = False
    last_large = 0  # the most recent 만/억/조, for the spoken-tail rule
    for token in _TOKEN_RE.findall(body):
        if token in _SMALL:
            section += (pending if pending is not None else Decimal(1)) * _SMALL[token]
            pending = None
            saw_unit = True
        elif token in _LARGE:
            chunk = section + (pending if pending is not None else Decimal(0))
            if chunk == 0:
                chunk = Decimal(1)
            total += chunk * _LARGE[token]
            section = Decimal(0)
            pending = None
            saw_unit = True
            last_large = _LARGE[token]
        else:
            number = _to_decimal(token)
            if number is None:
                return None
            if pending is not None:  # two numbers in a row: "3억 5000" -> keep adding
                section += pending
            pending = number
    if spoken and section and pending is None and last_large >= _LARGE["억"]:
        section *= last_large // _LARGE["만"]  # "2억 8천" → 8천만, "1조 2천" → 2천억
    if spoken and not section and pending is not None and last_large >= _LARGE["억"]:
        pending *= last_large // _LARGE["만"]  # "2억 4000" → 4000만 (the 만 is left out)
    total += section + (pending if pending is not None else Decimal(0))
    if not saw_unit and total == 0:
        return None
    return int(total)


_BARE_NUMBER_RE = re.compile(r"(?<![\d,.])\d{1,3}(?:,\d{3})+(?![\d,])")


def find_amounts(
    text: str, *, default_unit: int = 1, bare_numbers: bool = False
) -> list[AmountMatch]:
    """Find every won amount in ``text``.

    Bare numbers are only treated as money when followed by 원 — unless ``bare_numbers`` is set,
    which is how budget-table cells ("352,000" under "(단위: 천원)") are read: comma-grouped
    numbers are multiplied by ``default_unit``.
    """
    matches: list[AmountMatch] = []
    if bare_numbers:
        for m in _BARE_NUMBER_RE.finditer(text):
            tail = text[m.end() : m.end() + 2]
            if tail.startswith(("원", "천", "만", "억", "백")):
                continue  # has an explicit unit; handled by the span regex below
            bare_value = int(m.group(0).replace(",", "")) * default_unit
            matches.append(AmountMatch(bare_value, m.start(), m.end(), m.group(0), False))
    for m in _SPAN_RE.finditer(text):
        if m.group("plain") is not None:
            value = _to_decimal(m.group("plain"))
            if value is None:
                continue
            matches.append(AmountMatch(int(value), m.start(), m.end(), m.group(0), True))
            continue
        body = m.group("body")
        # Hangul digits are only trusted when attached to a large unit and followed by 원;
        # otherwise "일부", "이천" (a city) etc. would parse as numbers.
        if re.match(_HNUM, body) and not (m.group("won") and re.search("[만억조]", body)):
            continue
        value_int = _evaluate(body, spoken=not m.group("won"))
        if value_int is None:
            continue
        if not m.group("won") and not re.search("[만억조]", body):
            value_int *= default_unit
        matches.append(AmountMatch(value_int, m.start(), m.end(), m.group(0), bool(m.group("won"))))
    return matches


def parse_krw(text: str, *, default_unit: int = 1) -> int | None:
    """Parse the single most salient amount in ``text`` (the largest one)."""
    found = find_amounts(text, default_unit=default_unit)
    if not found:
        stripped = text.strip().replace(",", "")
        if stripped.isdigit():
            return int(stripped) * default_unit
        return None
    return max(found, key=lambda a: a.value).value


_UNIT_HEADER_RE = re.compile(r"\(?\s*단위\s*[:：]\s*(천원|백만원|만원|억원|원)\s*\)?")
_UNIT_HEADER_VALUES = {"원": 1, "천원": 1000, "만원": 10**4, "백만원": 10**6, "억원": 10**8}


def detect_table_unit(text: str) -> int | None:
    """Read the "(단위: 천원)" header that budget books put above tables."""
    m = _UNIT_HEADER_RE.search(text)
    return _UNIT_HEADER_VALUES[m.group(1)] if m else None


def amounts_agree(a: int, b: int, *, tolerance: float = 0.01) -> bool:
    if a == b:
        return True
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) / max(a, b) <= tolerance


def format_krw(value: int) -> str:
    """350000000 -> '3억 5,000만원'; 50000000 -> '5,000만원'."""
    if value <= 0:
        return "0원"
    eok, rest = divmod(value, 10**8)
    man, won = divmod(rest, 10**4)
    parts: list[str] = []
    if eok:
        parts.append(f"{eok:,}억")
    if man:
        parts.append(f"{man:,}만")
    if won and not eok:
        parts.append(f"{won:,}")
    return " ".join(parts) + "원"
