"""Conservative purchase patterns for budget explanations and structured work plans.

No institution-specific rules or reference labels. Each amount belongs to its own
clause; missing or ambiguous amounts stay null. Callers must establish the speaker
before passing a council answer. Quotes remain literal source substrings.
"""

from __future__ import annotations

import re

from app.domain.krw import detect_table_unit, find_amounts, parse_krw
from app.domain.taxonomy import Category, classify_budget_category
from app.domain.timing import resolve_timing
from app.llm.prompts import ChunkContext
from app.llm.schemas import Commitment, ExtractedSignal

_PURCHASE = re.compile(
    r"구입|구매|교체|구축|도입|설치|고도화|신축|증축|건립|공사|리모델링|용역|제작"
)
_ALLOCATION = re.compile(r"(?:계상|편성|반영)(?:하였|했|하겠)")
_COMPLETED = re.compile(r"(?:집행|설치|구입|구매|교체|공사)\s*완료|완료하였|완료했")
_UNSAFE_ALLOCATION = re.compile(
    r"지난해|작년|당시|이미|취소|중지|중단|보류|반납|되면|된다면|경우|조건|확보\s*시|하지\s*않|못했"
)
_ITEM_END = re.compile(
    r"(?:구입|구매|교체|구축|도입|설치|고도화|신축|증축|건립|공사|리모델링)"
    r"(?:비용|사업비|비)?(?:으로|로|에)?\s*$"
)
_NO_PURCHASE = re.compile(r"유지\s*(?:보수|관리)|지원금|격려금|출연금|전출금|상환|공공요금|수수료")
_ASSET_CATEGORIES = (
    (Category.AI_DATA, re.compile(r"ChatGPT|생성형\s*AI|인공지능", re.I)),
    (
        Category.PUBLIC_SW,
        re.compile(r"방화벽|백업|전산|컴퓨터|태블릿|단말기|서버|행정망|VPN", re.I),
    ),
    (Category.FACILITY, re.compile(r"청소차|회의실|의자|책상|방수공사|개선공사")),
)


def purchase_category(title: str) -> Category:
    for category, pattern in _ASSET_CATEGORIES:
        if pattern.search(title):
            return category
    return classify_budget_category(title, "")[0]


def _signal(
    ctx: ChunkContext,
    title: str,
    evidence: list[str],
    commitment: Commitment,
    amount_text: str | None = None,
    amount: int | None = None,
    *,
    budget_document: bool = False,
    timing_source: str | None = None,
) -> ExtractedSignal:
    timing = resolve_timing(
        " ".join(evidence) if timing_source is None else timing_source, ctx.document_date
    )
    category = purchase_category(title)
    return ExtractedSignal(
        title=title,
        summary=f"{ctx.institution or '기관'}의 {title} ({commitment})",
        category=category,
        institution_mention=ctx.institution,
        department=None,
        budget_text=amount_text,
        budget_krw=amount,
        timing_text=timing.basis if timing else None,
        expected_year=timing.year if timing else (ctx.fiscal_year if budget_document else None),
        expected_half=timing.half if timing else None,
        commitment=commitment,
        procurement_type="unknown",
        keywords=[title],
        evidence=evidence,
        confidence=0.35 if category is Category.OTHER else 0.65,
    )


def allocated_purchases(ctx: ChunkContext, answer: str) -> list[ExtractedSignal]:
    """One explicit asset/amount pair per committed clause, never the largest speech amount."""
    signals: list[ExtractedSignal] = []
    for raw_sentence in re.findall(r"[^.!?。]+[.!?。]?", answer):
        sentence = raw_sentence.strip()
        if (
            not _ALLOCATION.search(sentence)
            or _COMPLETED.search(sentence)
            or _UNSAFE_ALLOCATION.search(sentence)
        ):
            continue
        amounts = find_amounts(sentence)
        previous_end = 0
        for amount in amounts:
            prefix = sentence[previous_end : amount.start]
            previous_end = amount.end
            # Boundary at a clause separator, not the comma inside a money token.
            prefix = re.split(r",\s+|(?:원)?과\s+|(?:원)?및\s+", prefix)[-1]
            prefix = re.sub(r"^(?:또한|다음으로|그리고)\s+", "", prefix.strip())
            prefix = re.sub(r"^.*?(?:취득비로|구입하고자|교체하고자)\s+", "", prefix)
            title = re.sub(
                r"(?:비용|사업비|구입비|설치비|교체비|비)?(?:으로|로|에)?\s*$", "", prefix
            )
            title = title.strip(" ,")
            if not _ITEM_END.search(prefix) or _NO_PURCHASE.search(prefix) or len(title) < 3:
                continue
            # Every item retains the shared predicate that authorizes the allocation.
            signals.append(
                _signal(
                    ctx,
                    title,
                    [sentence],
                    "committed",
                    amount.raw,
                    amount.value,
                    timing_source=prefix,
                )
            )
    return signals


_PLAN_HEADING = re.compile(r"(?m)^\s*\d+\.\s+(?P<title>[^\n]+)")
_SECTION = re.compile(r"(?m)^\s*[❏□■▣]\s*(?P<name>[^\n]+)")


def work_plan_signals(ctx: ChunkContext) -> list[ExtractedSignal]:
    """Numbered work-plan sections; never turn historical implementation into new work."""
    headings = list(_PLAN_HEADING.finditer(ctx.text))
    signals: list[ExtractedSignal] = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(ctx.text)
        block = ctx.text[heading.end() : end]
        sections = list(_SECTION.finditer(block))
        values = {
            s.group("name").strip(): block[
                s.end() : sections[i + 1].start() if i + 1 < len(sections) else len(block)
            ].strip()
            for i, s in enumerate(sections)
        }
        if "사업개요" not in values or not any("소요예산" in name for name in values):
            continue
        title = heading.group("title").strip()
        plan = values.get("추진계획", "")
        if re.search(
            r"취소|중지|중단|보류|철회|추진하지|사업\s*종료|(?:계획|예정)[은이도]?\s*없", plan
        ):
            continue
        # A paid subscription expansion is a purchase; routine 운영 is not.
        if not _PURCHASE.search(title) and not (
            re.search(r"유료|계정", values["사업개요"]) and re.search(r"추가|확대", plan)
        ):
            continue
        if _NO_PURCHASE.search(title):
            continue
        evidence = [heading.group(0).strip()]
        timing_line = next(
            (line.strip() for line in values["사업개요"].splitlines() if "사업기간" in line), None
        )
        if timing_line:
            evidence.append(timing_line)
        costs = "\n".join(value for name, value in values.items() if "소요예산" in name)
        if "기집행" in costs:
            continue
        amounts = find_amounts(costs)
        unique = amounts[0] if len(amounts) == 1 else None
        if unique:
            evidence.append(next(line.strip() for line in costs.splitlines() if unique.raw in line))
        signals.append(
            _signal(
                ctx,
                title,
                evidence,
                "planned",
                unique.raw if unique else None,
                unique.value if unique else None,
                budget_document=True,
            )
        )
    return signals


_REVIEW_ROW = re.compile(
    r"(?m)^\s*\(\d+p\)\s*(?P<title>[^\n]+?)\s+(?P<amount>\d[\d,]*)"
    r"[ \t]*(?:\n[ \t]*)?(?:-|\d[\d,]*)\s+[△−-]?\d[\d,]*\s+(?:순증|[\d.]+)"
)


def review_table_signals(ctx: ChunkContext, unit: int) -> list[ExtractedSignal]:
    if not all(word in ctx.text for word in ("사업명", "예산액", "기정액")):
        return []
    signals = []
    for row in _REVIEW_ROW.finditer(ctx.text):
        title = row.group("title").strip()
        if not _PURCHASE.search(title) or _NO_PURCHASE.search(title):
            continue
        amount = parse_krw(row.group("amount"), default_unit=unit)
        if amount:
            signals.append(
                _signal(
                    ctx,
                    title,
                    [row.group(0).strip()],
                    "committed",
                    row.group("amount"),
                    amount,
                    budget_document=True,
                )
            )
    return signals


_REVIEW_HEADING = re.compile(r"(?m)^[ \t\f]*\d+\)[ \t]+(?P<title>[^\n]+)")
_REVIEW_TOTAL = re.compile(r"(?m)^[ \t]*계[ \t]+(?P<amount>\d[\d,]*)[ \t]+")


def numbered_review_signals(ctx: ChunkContext) -> list[ExtractedSignal]:
    """Read current-budget totals inside independently headed review sections.

    Require the current/prior-year column order and an explicit local unit.
    Rounded prose and component costs must not replace the table total.
    """
    headings = list(_REVIEW_HEADING.finditer(ctx.text))
    signals: list[ExtractedSignal] = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(ctx.text)
        block = ctx.text[heading.end() : end]
        title = re.sub(r"\s*\((?:신규|계속|p\.\s*\d+)\)", "", heading.group("title")).strip()
        if not _PURCHASE.search(title) or _NO_PURCHASE.search(title):
            continue
        if re.search(r"사업\s*(?:취소|철회|중단)|편성하지|전액\s*삭감", block):
            continue
        header = re.search(r"재원별\s+예산액\s+전년도당초예산액", block)
        totals = list(_REVIEW_TOTAL.finditer(block))
        unit = detect_table_unit(block)
        if not header or len(totals) != 1 or unit is None:
            continue
        total = totals[0]
        if total.start() < header.end():
            continue
        amount = parse_krw(total.group("amount"), default_unit=unit)
        if not amount:
            continue
        signals.append(
            _signal(
                ctx,
                title,
                [heading.group(0).strip(), total.group(0).strip()],
                "planned",
                total.group("amount"),
                amount,
                budget_document=True,
            )
        )
    return signals
