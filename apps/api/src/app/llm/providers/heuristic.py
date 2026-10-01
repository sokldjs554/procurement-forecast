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

from app.domain.krw import detect_table_unit, find_amounts, parse_krw
from app.domain.speakers import OFFICIAL_ENDINGS
from app.domain.taxonomy import (
    CATEGORIES,
    COMMITMENT_LADDER,
    PROCUREMENT_VERBS,
    Category,
    classify_budget_category,
    classify_category,
    commitment_level,
)
from app.domain.timing import resolve_timing
from app.llm.prompts import EXTRACT_PROMPT_VERSION, BriefFacts, ChunkContext
from app.llm.providers.procurement_narratives import (
    allocated_purchases,
    numbered_review_signals,
    purchase_category,
    review_table_signals,
    work_plan_signals,
)
from app.llm.schemas import Commitment, ExtractedSignal, ExtractionOutput
from app.llm.types import LLMResult
from app.parsing.chunking import budget_project_row, chunk_budget, chunk_minutes, match_speaker

HEURISTIC_VERSION = "heuristic-v10"

_SENTENCE_RE = re.compile(r"[^.?!。]+[.?!。]?")
_SPEAKER_LABEL_RE = re.compile(r"(?P<role>[가-힣A-Za-z·]{1,20}) [가-힣]{2,4}")
_MAINTENANCE_RE = re.compile(r"유지\s*(?:관리|보수)")
_NEW_PURCHASE_RE = re.compile(
    r"설치|구축|조성|도입|교체|보급|전환|확충|리모델링|고도화|구매|구입|임차|신축|증축|개축|개발|건립"
)
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
    "구입",
    "구매",
    "임차",
    "용역",
    "공사",
    "수립",
    "대체 취득",
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
_WAGE_ROW_RE = re.compile(r"(?:근로자|직원|공무원|인력)\s*(?:등\s*)?보수(?:\s|$)")
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
    "별도",
    "또한",
    "그리고",
    "다음으로",
)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_RE.findall(text) if s.strip()]


def _is_official_role(role: str) -> bool:
    return role.endswith(OFFICIAL_ENDINGS) and not role.endswith(("위원", "의원", "위원장", "의장"))


def _answer_text(chunk_text: str, labels: list[str] | None = None) -> tuple[str, str]:
    """Split an exchange into (question, answer) by speaker role."""
    question: list[str] = []
    answer: list[str] = []
    current = question
    lines = chunk_text.splitlines()
    headings = [
        match_speaker(line.strip())
        for line in lines
        if match_speaker(line.strip()) or line.lstrip().startswith(("○", "◯", "◎"))
    ]
    # Only an entirely unattributed audio chunk may supply review candidates.
    # In a mixed exchange, a known official's quote must never lend authority to
    # the placeholder speaker's budget or commitment.
    audio_only = bool(headings) and all(
        heading is not None
        and heading.group("role") == "발언자"
        and heading.group("name") == "미상"
        for heading in headings
    )
    # Long speeches lose their heading after chunk_minutes splits them. Only an
    # unambiguous canonical speaker label can supply the missing role; explicit
    # headings always take precedence, and neither text nor evidence is rewritten.
    if (
        labels
        and len(labels) == 1
        and (speaker := _SPEAKER_LABEL_RE.fullmatch(labels[0]))
        and _is_official_role(speaker.group("role"))
        and not headings
    ):
        current = answer
    for line in lines:
        content = line
        if speaker := match_speaker(line.strip()):
            content = speaker.group("speech")
            # Audio-only transcripts deliberately use this explicit placeholder.
            # Keep their inline candidate for grounding's needs_review route;
            # this is not executive attribution and never changes source labels.
            audio_unknown = (
                audio_only
                and speaker.group("role") == "발언자"
                and speaker.group("name") == "미상"
                and bool(content)
                and line.lstrip().startswith(("○", "◯", "◎"))
            )
            current = (
                answer if _is_official_role(speaker.group("role")) or audio_unknown else question
            )
        elif line.lstrip().startswith(("○", "◯", "◎")):
            current = question
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
_NAMED_SUBJECT_RE = re.compile(
    r"((?:[가-힣A-Za-z0-9·]+\s){0,5}[가-힣A-Za-z0-9·]+?)(?:은|는)(?=\s|요[?.]|[?.])"
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
        for m in _NAMED_SUBJECT_RE.finditer(source):
            if (
                not _NEW_PURCHASE_RE.search(source)
                and not re.search(r"발주|임차|착수|조달|확충|적용|개편", source)
                and _answer_commitment(source) not in {"reviewing", "declined"}
            ):
                continue
            candidate = _clean_title(m.group(1))
            _, conf = classify_category(candidate)
            if len(candidate) >= 3 and conf > 0 and _meaningful_title(candidate):
                return candidate
    return None


def _guess_project_title(answer: str, question: str) -> str | None:
    for source in (answer, question):
        best: str | None = None
        for m in _TITLE_RE.finditer(source):
            candidate = _with_relative_clause(source, m.start(1), _clean_title(m.group(1)))
            if suffix := re.match(
                r"\s+(?:구입|구매|구축|교체|설치|도입|공사|용역)(?:\s+공사)?", source[m.end() :]
            ):
                candidate += suffix.group(0)
            if len(candidate) < 3:
                continue
            if not _meaningful_title(candidate):
                continue
            _, conf = classify_category(candidate)
            relevant = (
                conf > 0
                or any(v in candidate for v in PROCUREMENT_VERBS)
                or bool(_NEW_PURCHASE_RE.search(candidate))
            )
            if relevant and (best is None or len(candidate) > len(best)):
                best = candidate
        if best:
            return best
    return None


def _meaningful_title(title: str) -> bool:
    words = re.sub(
        r"구입|구매|구축|교체|설치|도입|공사|용역|사업|추진|수립|신규|별도|및|말까지|원씩|"
        r"들어서|지금|단계에서는|계획|예정",
        "",
        title,
    )
    return len(re.sub(r"[\s\d,.]", "", words)) >= 2


def _answer_commitment(text: str) -> str | None:
    if re.search(r"어렵고|어렵습니다|곤란합니다|추진하지\s*않|보류|불가", text):
        return "declined"
    if re.search(r"협의.*추진\s*여부|확정되면|확보되면|검토해\s*보겠습니다", text):
        return "reviewing"
    if re.search(r"확보(?:돼|되어)\s*있|확보해서|발주합니다", text):
        return "committed"
    if level := commitment_level(text):
        return level
    if re.search(
        r"(?:구입|구매|교체|설치|도입|구축|추진|편성|발주|용역)(?:하려고|할\s*예정|할\s*것)|"
        r"예산을\s*세우|책정돼|계획을\s*세우",
        text,
    ):
        return "planned"
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
    official = bool(
        len(ctx.labels) == 1
        and (label := _SPEAKER_LABEL_RE.fullmatch(ctx.labels[0]))
        and _is_official_role(label.group("role"))
        and not any(
            match_speaker(line.strip()) or line.lstrip().startswith(("○", "◯", "◎"))
            for line in lines
        )
    )
    signals: list[ExtractedSignal] = []
    for i, raw in enumerate(lines):
        line = raw.strip()
        if speaker := match_speaker(line):
            official = _is_official_role(speaker.group("role"))
            line = speaker.group("speech")
        elif line.startswith(("○", "◯", "◎")):
            official = False
        if not official:
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


def _extract_exchange(ctx: ChunkContext, *, split: bool = True) -> list[ExtractedSignal]:
    if split:
        exchanges = chunk_minutes(ctx.text)
        if sum(chunk.kind == "exchange" for chunk in exchanges) > 1:
            return [
                signal
                for chunk in exchanges
                for signal in _extract_exchange(
                    ChunkContext(
                        ctx.doc_type,
                        ctx.title,
                        ctx.institution,
                        ctx.document_date,
                        chunk.labels,
                        chunk.text,
                        ctx.fiscal_year,
                    ),
                    split=False,
                )
            ]
    if listed := _budget_list_signals(ctx):
        return listed
    question, answer = _answer_text(ctx.text, ctx.labels)
    if not answer:
        return []
    if _MAINTENANCE_RE.search(answer) and not _NEW_PURCHASE_RE.search(question + " " + answer):
        return []
    segments = _project_answers(answer)
    signals = []
    for segment in segments:
        signals.extend(_extract_project(ctx, question, segment))
    return signals


_COMPLETED_PROJECT = re.compile(
    r"기집행|"
    r"(?:구축|설치|구입|구매|교체|준공)(?:했|하였|을\s*완료|가\s*완료)|"
    r"(?:설치|구축|공사)\s*완료"
)
_EXPLICIT_FUTURE = re.compile(r"내년|내후년|향후|앞으로|할\s*(?:계획입니다|예정)|하겠습니다")
_CANCELLED_PROJECT = re.compile(r"(?:사업[은을이]?\s*.*)?(?:취소|철회)(?:되|했|하였)|전액\s*삭감")
_NONPURCHASE = re.compile(
    r"(?:설치비|구입비|비용)[를을]?\s*(?:일부\s*)?지원|"
    r"(?:가구|명|인)당.*지원|장학금|장학기금|출연금|생활안정자금"
)


def _project_answers(answer: str) -> list[str]:
    """Keep future project clauses together, without money from completed work.

    Split only on two distinct, named purchase commitments. A generic amount or
    follow-up timing sentence stays with its project. Denial remains in scope;
    cancellation of an unnamed '해당 사업' makes the whole answer unsafe.
    """
    cancelled_titles = []
    for sentence in _sentences(answer):
        if _CANCELLED_PROJECT.search(sentence):
            title = _guess_project_title(sentence, "")
            if not title or re.search(r"(?:해당|이|그)\s*사업", title):
                return []
            cancelled_titles.append(re.sub(r"\s+", "", title))
    sentences = []
    for sentence in _sentences(answer):
        if any(title in re.sub(r"\s+", "", sentence) for title in cancelled_titles):
            continue
        # Negative evidence must survive background filtering so it reaches the
        # commitment decision and the production grounding verifier unchanged.
        if _answer_commitment(sentence) == "declined":
            sentences.append(sentence)
            continue
        historical = bool(re.search(r"지난해|작년", sentence)) and not _EXPLICIT_FUTURE.search(
            sentence
        )
        if (
            historical
            or _COMPLETED_PROJECT.search(sentence)
            or _NONPURCHASE.search(sentence)
            or re.search(r"확보되면.*(?:계상|편성|구입|구매)", sentence)
            or (_MAINTENANCE_RE.search(sentence) and not _NEW_PURCHASE_RE.search(sentence))
        ):
            continue
        sentences.append(sentence)
    if not sentences:
        return []
    starts: list[int] = []
    previous_title = ""
    for index, sentence in enumerate(sentences):
        title = _guess_title(sentence, "")
        explicitly_enumerated = re.match(r"(?:첫째|둘째|셋째|넷째|다섯째)\s*[,，]", sentence)
        if (
            not title
            or not (_NEW_PURCHASE_RE.search(title) or explicitly_enumerated)
            or not _answer_commitment(sentence)
        ):
            continue
        # The same named project can be explained in successive sentences.
        compact = re.sub(r"\s+", "", title)
        if previous_title and (compact in previous_title or previous_title in compact):
            continue
        starts.append(index)
        previous_title = compact
    if len(starts) < 2:
        return [" ".join(sentences)]
    starts[0] = 0
    return [
        " ".join(sentences[start : starts[i + 1] if i + 1 < len(starts) else len(sentences)])
        for i, start in enumerate(starts)
    ]


def _extract_project(ctx: ChunkContext, question: str, answer: str) -> list[ExtractedSignal]:
    if allocated := allocated_purchases(ctx, answer):
        for signal in allocated:
            signal.title = _clean_title(signal.title)
        return allocated
    # Renewing or reallocating maintenance budgets is not a new purchase. Keep
    # mixed discussions when a concrete purchase action is also stated.
    if _MAINTENANCE_RE.search(answer) and not _NEW_PURCHASE_RE.search(question + " " + answer):
        return []
    level = _answer_commitment(answer)
    if level is None:
        return []
    title = _guess_title(answer, question)
    focused = [
        candidate
        for sentence in _sentences(answer)
        if _answer_commitment(sentence) is not None
        and (candidate := _guess_project_title(sentence, ""))
        and _NEW_PURCHASE_RE.search(candidate)
    ]
    if focused:
        title = max(focused, key=len)
    question_title = _guess_project_title(question, "")
    if question_title and re.search(r"(?:이|그)\s*(?:예산|사업)(?:에서|은|는|의)", answer):
        title = question_title
    if title is None:
        return []
    # The project name is the best evidence of its category; the surrounding exchange often
    # drifts to other programmes (a parking question that mentions 어르신 이동 편의).
    category, cat_conf = classify_category(title)
    purchase_cat = purchase_category(title)
    if purchase_cat is not Category.OTHER and (category is Category.OTHER or cat_conf < 0.5):
        category, cat_conf = purchase_cat, max(cat_conf, 0.65)
    if cat_conf < 0.5:
        category, cat_conf = classify_category(question + " " + answer)
    if category is Category.OTHER and not _NEW_PURCHASE_RE.search(title):
        return []
    sentences = _sentences(answer)
    ladder = [p for phrases in COMMITMENT_LADDER.values() for p in phrases]
    evidence = [s for s in sentences if any(p in s for p in ladder) or _answer_commitment(s)]
    title_sentence = next((s for s in sentences if title in s), None)
    if title_sentence and title_sentence not in evidence:
        evidence.insert(0, title_sentence)
    # A full rollout estimate or a previous purchase is not the current budget.
    # Distinct remaining amounts are ambiguous: do not pick the largest one.
    budget_sentences = [
        s
        for s in sentences
        if not re.search(r"(?:다고\s*)?하면|경우|가정|지난해|작년|기집행|취소|반납", s)
    ]
    candidates = [
        (amount, s)
        for s in budget_sentences
        for amount in find_amounts(s)
        if not re.match(r"\s*(?:이상|이하|초과|미만)", s[amount.end :])
    ]
    totals = [
        (amount, s)
        for amount, s in candidates
        if re.search(r"(?:총|총사업비|총예산)(?:은|는|이|가)?\s*$", s[: amount.start])
    ]
    if totals:
        candidates = totals
    values = {amount.value for amount, _ in candidates}
    budget = candidates[0][0] if len(values) == 1 else None
    amount_sentence = candidates[0][1] if budget else None
    if amount_sentence and amount_sentence not in evidence:
        evidence.append(amount_sentence)
    timing = None
    if level in ("committed", "planned"):
        timing = resolve_timing(answer, ctx.document_date)
        # A building's opening or a previous implementation year is not a new
        # procurement date. Unresolved timing remains unknown for review.
        if timing and timing.year < ctx.document_date.year:
            timing = None
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
            confidence=0.35 if category is Category.OTHER else round(min(confidence, 0.85), 2),
        )
    ]


def _extract_budget_line(ctx: ChunkContext, table_unit: int) -> list[ExtractedSignal]:
    """Extract one row after structural splitting; no neighboring project text is used."""
    first_line = ctx.text.splitlines()[0] if ctx.text else ""
    row = budget_project_row(first_line)
    if row is None:
        return []
    name, amount_text = row
    if _WAGE_ROW_RE.search(name):
        return []
    if any(w in name for w in _OPERATING_WORDS) and not any(
        v in name for v in ("설치", "구축", "조성", "도입", "교체", "보급", "전환", "리모델링")
    ):
        return []
    category, conf = classify_budget_category(name, "\n".join(ctx.text.splitlines()[1:]))
    amount_krw = parse_krw(amount_text, default_unit=table_unit)
    if not amount_krw:
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
            budget_text=amount_text,
            budget_krw=amount_krw,
            timing_text=None,
            expected_year=ctx.fiscal_year,
            expected_half=None,
            commitment="committed",
            procurement_type="unknown",
            keywords=_keywords(name) or [name],
            evidence=[first_line.strip()],
            # Keep unresolved classification visible to the existing grounding review gate.
            confidence=0.35 if category is Category.OTHER else round(0.7 + 0.1 * min(conf, 1.0), 2),
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
            signals = (
                work_plan_signals(ctx)
                + review_table_signals(ctx, unit)
                + numbered_review_signals(ctx)
            )
            for chunk in chunk_budget(ctx.text, standalone=True):
                row_context = ChunkContext(
                    ctx.doc_type,
                    ctx.title,
                    ctx.institution,
                    ctx.document_date,
                    chunk.labels or ctx.labels,
                    chunk.text,
                    ctx.fiscal_year,
                )
                signals.extend(_extract_budget_line(row_context, unit))
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
