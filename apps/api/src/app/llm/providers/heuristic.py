"""Rule-based extractor — the offline fallback and the evaluation baseline.

It exists for three reasons: the product must keep producing (lower-confidence) signals when
the LLM is down or the daily budget is exhausted; the demo and CI must run without API keys;
and every LLM prompt change is measured against a floor that costs nothing.

It understands the two shapes that matter most — council exchanges and budget-book lines — using
the same taxonomy, commitment ladder, amount parser and timing resolver as the verifier.
"""

from __future__ import annotations

import re
from typing import cast

from app.domain.krw import detect_table_unit, find_amounts
from app.domain.taxonomy import (
    CATEGORIES,
    COMMITMENT_LADDER,
    PROCUREMENT_VERBS,
    Category,
    classify_category,
    commitment_level,
)
from app.domain.timing import resolve_timing
from app.llm.prompts import EXTRACT_PROMPT_VERSION, BriefFacts, ChunkContext
from app.llm.schemas import Commitment, ExtractedSignal, ExtractionOutput
from app.llm.types import LLMResult
from app.parsing.chunking import GLUED_MEMBER_RE

HEURISTIC_VERSION = "heuristic-v2"

_SENTENCE_RE = re.compile(r"[^.?!。]+[.?!。]?")
_SPEAKER_PREFIX_RE = re.compile(r"^[○◯◎]\s*[가-힣A-Za-z·]+\s+[가-힣]{2,4}\s+")
_PROJECT_SUFFIX = (
    "설치",
    "구축",
    "조성",
    "도입",
    "교체",
    "보급",
    "전환",
    "확충",
    "리모델링",
    "고도화",
    "플랫폼",
    "시스템",
    "서비스",
    "사업",
)
_TITLE_RE = re.compile(
    r"((?:[가-힣A-Za-z0-9·()]+\s){0,4}[가-힣A-Za-z0-9·()]*(?:"
    + "|".join(_PROJECT_SUFFIX)
    + r"))(?=[은는이가을를의에도\s,.]|$)"
)
_OPERATING_WORDS = (
    "운영",
    "유지관리",
    "급여",
    "업무추진비",
    "개최",
    "인건비",
    "수당",
    "지원",
    "기본경비",  # every 부서's 행정운영경비 row in real books
)
_BUDGET_LINE_RE = re.compile(
    r"세\s*부\s*사\s*업\s*[:：]?\s*(?P<name>.+?)\s+(?P<amount>\d{1,3}(?:,\d{3})+|\d{4,})"
)
# Real 세출예산사업명세서 rows carry no "세부사업" label: "상권활성화재단 출연 4,039,360
# 3,864,360 175,000" (예산액, 전년도/기정액, 증감 with △ for a cut). The chunker only starts a
# chunk on such a row when a 편성목 follows it, which is what separates it from subtotals.
_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)"
_TABLE_ROW_RE = re.compile(
    rf"^\s*(?P<name>[가-힣A-Za-z(][^\n]*?)\s+(?P<amount>{_NUM})\s+{_NUM}\s+△?{_NUM}\s*$"
)
_MEMBER_ROLES = ("위원", "의원", "위원장", "의장")
_GENERIC_TITLE_HEADS = (
    "예산",
    "사업비",
    "기본계획",
    "사업",
    "내년도",
    "올해",
    "현재",
    "위원님",
    "말씀",
)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_RE.findall(text) if s.strip()]


def _answer_text(chunk_text: str) -> tuple[str, str]:
    """Split an exchange into (question, answer) by speaker role."""
    question: list[str] = []
    answer: list[str] = []
    current = question
    for line in chunk_text.splitlines():
        content = line
        if glued := GLUED_MEMBER_RE.match(line.strip()):
            current, content = question, glued.group("speech")
        elif m := re.match(r"^[○◯◎]\s*(?P<role>[가-힣A-Za-z·]+)\s+[가-힣]{2,4}\s+", line.strip()):
            current = question if m.group("role") in _MEMBER_ROLES else answer
            content = _SPEAKER_PREFIX_RE.sub("", line.strip())
        current.append(content)
    return " ".join(question), " ".join(answer)


_TIMING_HEAD_RE = re.compile(
    r"^(?:\d{2,4}년(?:도)?|'\d{2}년|내년(?:도)?|올해|금년|내후년|이번|상반기|하반기|본예산|추경|제\d회)[가-힣]*$"
)


# "현재 재정 여건상 스마트쉘터 설치는 …" — circumstance qualifiers are not part of the name.
_QUALIFIER_HEAD_RE = re.compile(
    r"^(?:(?:현재|당분간|우선|일단)\s+)*(?:(?:재정|예산|행정|인력)\s*)?(?:여건|사정|형편)상\s+"
)
# Verb forms that make the preceding words a relative clause ("먼저 잡아주는 CCTV").
_ADNOMINAL_RE = re.compile(r"[가-힣]+(?:주는|하는|되는|잡는|있는|없는|알리는|막는)$")
_CLAUSE_ARG_RE = re.compile(r"^[가-힣A-Za-z0-9·]+(?:을|를|이|가|의)$")


def _with_relative_clause(source: str, start: int, candidate: str) -> str:
    """Keep a spoken relative clause whole instead of starting the name mid-clause.

    "AI가 이상행동을 먼저 잡아주는 CCTV는 …" matches as "먼저 잡아주는 CCTV"; walking left over the
    clause's arguments (words ending in 을/를/이/가/의) recovers "AI가 이상행동을 먼저 잡아주는 CCTV".
    """
    words = candidate.split()
    if not any(_ADNOMINAL_RE.fullmatch(w) for w in words[:-1]):
        return candidate
    before = re.split(r"[.,!?·\n]", source[:start])[-1].split()
    taken: list[str] = []
    while before and len(taken) < 3 and _CLAUSE_ARG_RE.fullmatch(before[-1]):
        taken.insert(0, before.pop())
    return " ".join([*taken, *words])


def _clean_title(title: str) -> str:
    """Drop leading timing/filler words: "내년 하반기에 정보시스템 클라우드 전환" → "정보시스템 클라우드 전환"."""
    title = _QUALIFIER_HEAD_RE.sub("", title)
    words = title.split()
    while words and (
        words[0] in _GENERIC_TITLE_HEADS
        or _TIMING_HEAD_RE.match(words[0])
        or words[0].endswith(("에", "로", "으로", "에서"))
        or re.fullmatch(r"\d+[가-힣]*", words[0])
    ):
        words.pop(0)
    return " ".join(words).strip(" ,.")


_SUBJECT_RE = re.compile(
    r"((?:[가-힣A-Za-z0-9·]+\s){0,2}[가-힣A-Za-z0-9·]+?)(?:은|는|을|를)\s(?:필요성|도입|운영|추진|설치)"
)


def _guess_title(answer: str, question: str) -> str | None:
    title = _guess_project_title(answer, question)
    if title:
        return title
    # Spoken subjects often lack a project suffix ("3차원 디지털트윈은 필요성은 공감합니다만").
    for source in (answer, question):
        for m in _SUBJECT_RE.finditer(source):
            candidate = _with_relative_clause(source, m.start(1), _clean_title(m.group(1)))
            _, conf = classify_category(candidate)
            if len(candidate) >= 2 and conf > 0:
                return candidate
    return None


def _guess_project_title(answer: str, question: str) -> str | None:
    for source in (answer, question):
        best: str | None = None
        for m in _TITLE_RE.finditer(source):
            candidate = _with_relative_clause(source, m.start(1), _clean_title(m.group(1)))
            if len(candidate) < 3:
                continue
            _, conf = classify_category(candidate)
            relevant = conf > 0 or any(v in candidate for v in PROCUREMENT_VERBS)
            if relevant and (best is None or len(candidate) > len(best)):
                best = candidate
        if best:
            return best
    return None


def _keywords(text: str) -> list[str]:
    found: list[str] = []
    for info in CATEGORIES.values():
        for kw in info.keywords:
            if kw in text and kw not in found:
                found.append(kw)
    return found[:6]


# The 예산안 제안 설명 in 본회의 (and again in 예결특위) reads the budget's main projects as one list:
# "주요사업비 예산 반영 내역으로는 판교 시스템반도체 연구센터 조성 263억 원, 오리공원 물놀이장
# 설치 공사비 10억 원, … 등을 반영하였습니다." Each item is a project and its amount, so each is a
# signal; one signal for the whole speech would keep one garbled title and the largest amount.
_BUDGET_LIST_HEAD_RE = re.compile(r"(?:예산|사업비?)\s*반영\s*내역")
_LIST_ITEM_RE = re.compile(
    r"(?P<name>[가-힣A-Za-z0-9·()][가-힣A-Za-z0-9·()\s]*?)\s*(?:이|가|에)?\s*"
    r"(?P<amount>\d[\d,]*억(?:\s*\d[\d,]*만)?\s*원)"
)
# transfers, repayments and handouts are in the list too but are not projects anyone bids on
_LIST_SKIP_WORDS = (
    "전출금",
    "출연금",
    "상환",
    "환급금",
    "쿠폰",
    "지원금",
    "보전금",
    "융자",
    "상품권",
    "축하금",
)
_BUDGET_BILL_YEAR_RE = re.compile(
    r"20\d\d년도?\s*(?:제\s*\d+\s*회\s*)?(?:일반\s*및\s*특별회계\s*)?"
    r"(?:세입\s*[·ㆍ]?\s*세출\s*)?(?:추가경정)?\s*예산안"
)


def _budget_list_signals(ctx: ChunkContext) -> list[ExtractedSignal]:
    """One signal per item of a "주요사업비 예산 반영 내역" list spoken by the executive."""
    lines = ctx.text.splitlines()
    member = False
    signals: list[ExtractedSignal] = []
    for i, raw in enumerate(lines):
        line = raw.strip()
        if glued := GLUED_MEMBER_RE.match(line):
            member = True
            line = glued.group("speech")
        elif m := re.match(r"^[○◯◎]\s*(?P<role>[가-힣A-Za-z·]+)\s+[가-힣]{2,4}\s+", line):
            member = m.group("role") in _MEMBER_ROLES
            line = line[m.end() :]
        if member:
            continue
        head = _BUDGET_LIST_HEAD_RE.search(line)
        if head is None and not (i > 0 and _BUDGET_LIST_HEAD_RE.search(lines[i - 1])):
            continue
        body = line[head.end() :] if head else line
        # items are ", "-separated; "1,120억 원" has no space after its comma
        items = [it for it in re.split(r",\s+", body) if _LIST_ITEM_RE.search(it)]
        if len(items) < 3:
            continue
        bill = _BUDGET_BILL_YEAR_RE.search(ctx.text)
        timing = resolve_timing(bill.group(0), ctx.document_date) if bill else None
        for item in items:
            m = _LIST_ITEM_RE.search(item)
            assert m is not None
            name = re.sub(r"^(?:으로는|은|는|입니다\.?)\s*", "", m.group("name").strip())
            if name.endswith(("사업비", "공사비", "건립비", "조성비")):
                name = name[:-1]
            if len(name) < 4 or any(w in name for w in _LIST_SKIP_WORDS):
                continue
            category, conf = classify_category(name)
            if category is Category.OTHER:
                continue
            amounts = find_amounts(m.group("amount"))
            if not amounts:
                continue
            amount = amounts[0]
            signals.append(
                ExtractedSignal(
                    title=name,
                    summary=f"{ctx.institution or '기관'} 예산안 제안 설명: '{name}' {m.group('amount')}",
                    category=category,
                    institution_mention=ctx.institution,
                    department=None,
                    budget_text=m.group("amount"),
                    budget_krw=amount.value,
                    timing_text=bill.group(0) if bill and timing else None,
                    expected_year=timing.year if timing else None,
                    expected_half=None,
                    commitment="committed",
                    procurement_type="unknown",
                    keywords=_keywords(name) or [name],
                    evidence=[item[m.start() : m.end()].strip()],
                    confidence=round(0.65 + 0.1 * min(conf, 1.0), 2),
                )
            )
    return signals


def _extract_exchange(ctx: ChunkContext) -> list[ExtractedSignal]:
    if listed := _budget_list_signals(ctx):
        return listed
    question, answer = _answer_text(ctx.text)
    if not answer:
        return []
    level = commitment_level(answer)
    if level is None:
        return []
    title = _guess_title(answer, question)
    if title is None:
        return []
    # The project name is the best evidence of its category; the surrounding exchange often
    # drifts to other programmes (a parking question that mentions 어르신 이동 편의).
    category, cat_conf = classify_category(title)
    if cat_conf < 0.5:
        category, cat_conf = classify_category(question + " " + answer)
    if category is Category.OTHER:
        return []
    sentences = _sentences(answer)
    ladder = [p for phrases in COMMITMENT_LADDER.values() for p in phrases]
    evidence = [s for s in sentences if any(p in s for p in ladder)]
    amounts = find_amounts(answer)
    amount_sentence = next((s for s in sentences if find_amounts(s)), None)
    if amount_sentence and amount_sentence not in evidence:
        evidence.append(amount_sentence)
    budget = max(amounts, key=lambda a: a.value) if amounts else None
    timing = (
        resolve_timing(answer, ctx.document_date) if level in ("committed", "planned") else None
    )
    timing_text = None
    if timing is not None:
        idx = answer.find(timing.basis)
        timing_text = (
            answer[max(0, idx) : idx + len(timing.basis) + 6].strip() if idx >= 0 else None
        )
    confidence = 0.45 + 0.15 * (level in ("committed", "planned")) + 0.1 * bool(budget)
    confidence += 0.1 * min(cat_conf, 1.0)
    dept = None
    if m := re.search(r"○\s*([가-힣]+(?:과|담당관|센터))장\s", ctx.text):
        dept = m.group(1)
    return [
        ExtractedSignal(
            title=title,
            summary=f"{ctx.institution or '기관'}에서 {title} 관련 발언({level})",
            category=category,
            institution_mention=ctx.institution,
            department=dept,
            budget_text=budget.raw if budget else None,
            budget_krw=budget.value if budget else None,
            timing_text=timing_text,
            expected_year=timing.year if timing else None,
            expected_half=timing.half if timing else None,
            commitment=cast(Commitment, level),
            procurement_type="unknown",
            keywords=_keywords(question + " " + answer) or [title],
            evidence=evidence[:3] or sentences[-1:],
            confidence=round(min(confidence, 0.85), 2),
        )
    ]


def _extract_budget_line(ctx: ChunkContext, table_unit: int) -> list[ExtractedSignal]:
    first_line = ctx.text.splitlines()[0] if ctx.text else ""
    m = _BUDGET_LINE_RE.search(first_line) or _TABLE_ROW_RE.match(first_line)
    if not m:
        return []
    name = m.group("name").strip()
    if any(w in name for w in _OPERATING_WORDS) and not any(
        v in name for v in ("설치", "구축", "조성", "도입", "교체", "보급", "전환", "리모델링")
    ):
        return []
    category, conf = classify_category(ctx.text)
    if category is Category.OTHER:
        return []
    amount_k = int(m.group("amount").replace(",", ""))
    if amount_k == 0:
        return []  # cut to nothing in a 추경 ("사업 0 73,080 △73,080"): no longer a plan
    dept = next(
        (lbl.split(":", 1)[1].strip() for lbl in ctx.labels if lbl.startswith("부서")), None
    )
    return [
        ExtractedSignal(
            title=name,
            summary=f"{ctx.fiscal_year or ''}년도 예산서에 '{name}' 편성",
            category=category,
            institution_mention=ctx.institution,
            department=dept,
            budget_text=m.group("amount"),
            budget_krw=amount_k * table_unit,
            timing_text=None,
            expected_year=ctx.fiscal_year,
            expected_half=None,
            commitment="committed",
            procurement_type="unknown",
            keywords=_keywords(ctx.text) or [name],
            evidence=[first_line.strip()],
            confidence=round(0.7 + 0.1 * min(conf, 1.0), 2),
        )
    ]


class HeuristicProvider:
    name = "heuristic"
    extract_model = HEURISTIC_VERSION
    extract_effort: str | None = None
    brief_model = HEURISTIC_VERSION

    def __init__(self, table_unit_default: int = 1000) -> None:
        self._unit = table_unit_default

    async def extract(self, ctx: ChunkContext) -> LLMResult[ExtractionOutput]:
        if ctx.doc_type == "budget_book":
            unit = detect_table_unit(ctx.text) or self._unit
            signals = _extract_budget_line(ctx, unit)
        elif ctx.doc_type == "council_minutes":
            signals = _extract_exchange(ctx)
        else:
            signals = []
        return LLMResult(
            value=ExtractionOutput(signals=signals),
            provider=self.name,
            model=HEURISTIC_VERSION,
            prompt_version=EXTRACT_PROMPT_VERSION,
        )

    async def brief(self, facts: BriefFacts) -> LLMResult[str]:
        from app.pipeline.brief import template_brief

        return LLMResult(
            value=template_brief(facts),
            provider=self.name,
            model=HEURISTIC_VERSION,
            prompt_version="template-v3",
        )
