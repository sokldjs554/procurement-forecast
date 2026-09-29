"""Grounding verifier — the deterministic referee between the LLM and the database.

An extraction is only as trustworthy as the text it can point at. For every signal the LLM (or
the heuristic extractor) proposes, we check, without any model in the loop:

1. **Evidence** — each quote must be locatable in the source chunk: exact after whitespace/NFKC
   normalisation, or fuzzy (partial-ratio alignment) above a threshold that tolerates OCR noise.
   Offsets are mapped back to the original document for UI highlighting.
2. **Budget** — ``budget_krw`` must agree (±1%) with an amount parsed by :mod:`app.domain.krw`
   from the located evidence or the quoted budget phrase. The model never gets the last word on
   a number.
3. **Timing** — ``expected_year`` must follow from the quoted timing phrase and the meeting date.
4. **Council intent** — explicit denials, conditions, and reported speech cannot silently
   become an executive commitment. This is a conservative mismatch guard, not a semantic proof.

The verdict routes the signal: ``accepted`` goes straight to linking, ``needs_review`` lands in
the admin review queue, ``rejected`` is stored for eval but never shown to customers.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from itertools import pairwise
from typing import Any, Literal

from rapidfuzz import fuzz

from app.domain.krw import amounts_agree, find_amounts
from app.domain.text import collapse_ws, normalize_with_map
from app.domain.timing import resolve_timing
from app.parsing.chunking import split_turns

Verdict = Literal["accepted", "needs_review", "rejected"]

# Unknown roles stay reviewable; an unrecognised speaker is never an executive by default.
_OFFICIAL_ENDINGS = (
    "시장",
    "군수",
    "구청장",
    "국장",
    "과장",
    "실장",
    "팀장",
    "담당관",
    "소장",
    "본부장",
    "원장",
    "센터장",
    "관장",
    "사장",
    "대표이사",
)

# Narrow constructions, not individual negative words: "문제없습니다" and "차질 없이"
# describe a feasible plan. Historical denials ("없었습니다") are not current denials.
_DENIAL_RE = re.compile(
    r"(?:계획|예정)(?:은|이|도)?\s*(?:전혀\s*)?없(?:습니다|다|으며|고)"
    r"|(?:추진|발주|설치|구축|도입|구매|편성|반영|검토|착수)\s*하지\s*"
    r"(?:않겠습니다|않습니다|않고|않는다고)"
)
_CONDITIONAL_RE = re.compile(
    r"(?:확보|승인)(?:가|이)?\s*(?:되면|된다면|될\s*경우)"
    r"|가능하다면"
)
_REVIEW_FIRST_RE = re.compile(r"검토\s*(?:후|이후)")
_COMPLETED_ACTION_RE = re.compile(
    r"(?:추진|발주|설치|구축|도입|구매|편성|반영|착수|확보|확정|승인)"
    r"(?:하였습니다|했습니다|되었습니다|됐습니다)[.!。]?\s*$"
)
_QUOTED_RE = re.compile(r'"[^\"]*"|\'[^\']*\'|“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』')
_SENTENCE_END_RE = re.compile(r"[.!?。！？](?=\s|$)")
_QUESTION_RE = re.compile(r"[?？]\s*$|(?:습니까|까요|나요|는지요)[.!。]?\s*$")
_REPORTED_RE = re.compile(
    r"(?:라고|다고|다는|냐고|냐는)\s*(?:말씀|말했|질문|물으|물었|요청|요구|전달|인용)"
)
_DIRECT_INTENT_RE = re.compile(
    r"(?:추진|발주|설치|구축|도입|구매|편성|반영|검토|착수|확보|확정)"
    r"|계획|예정"
)


def _direct_evidence_statements(speech: str, start: int, end: int) -> list[str]:
    """Read only sentences touched by the located quote, with original source context.

    Mask quoted text before splitting sentences so punctuation inside a citation cannot
    turn the citation into a direct assertion. Offsets remain in the original speech.
    This deliberately leaves ambiguous reported speech for human review.
    """
    direct = _QUOTED_RE.sub(lambda match: " " * len(match[0]), speech)
    boundaries = [0, *(match.end() for match in _SENTENCE_END_RE.finditer(direct)), len(speech)]
    statements: list[str] = []
    for left, right in pairwise(boundaries):
        if right <= start or left >= end:
            continue
        original = collapse_ws(speech[left:right]).strip()
        statement = collapse_ws(direct[left:right]).strip()
        if _QUESTION_RE.search(statement) or _REPORTED_RE.search(statement):
            continue
        if original != statement and not _DIRECT_INTENT_RE.search(statement):
            continue
        if statement:
            statements.append(statement)
    return statements


def _commitment_evidence_issue(statements: list[str], commitment: str | None) -> str | None:
    # Do not relabel correctly extracted review/decline, or general non-council evidence.
    if commitment not in {"planned", "committed"}:
        return None
    if not any(len(statement.replace(" ", "")) >= 12 for statement in statements):
        return "commitment_context_ambiguous"
    if any(_DENIAL_RE.search(statement) for statement in statements):
        return "commitment_denied"
    if commitment == "committed" and any(
        _CONDITIONAL_RE.search(statement)
        or (_REVIEW_FIRST_RE.search(statement) and not _COMPLETED_ACTION_RE.search(statement))
        for statement in statements
    ):
        return "commitment_conditional"
    return None


def official_evidence_issue(
    document_text: str,
    checks: list[EvidenceCheck],
    *,
    char_start: int,
    commitment: str | None = None,
) -> str | None:
    """Attribute each located quote to ONE source turn, including continuation chunks.

    A member's question may supply the subject. At least one substantive quote must come
    from an executive answer. Strong claims also need to pass the explicit mismatch guard;
    passing it still does not establish complete semantic correctness.
    """
    turns = split_turns(document_text)
    official_quotes: list[str] = []
    statements: list[str] = []
    for check in checks:
        if not check.found or check.start is None or check.end is None:
            continue
        start, end = char_start + check.start, char_start + check.end
        turn = next((t for t in turns if t.start <= start and end <= t.end), None)
        if (
            turn
            and not turn.is_member
            and not turn.is_chair
            and not turn.role.endswith(("위원", "의원", "위원장", "의장"))
            and turn.role.endswith(_OFFICIAL_ENDINGS)
            # An unknown speaker heading must not inherit the preceding official's role.
            and not re.search(r"(?m)^[ \t]*[○◯◎]", document_text[turn.start + 1 : end])
        ):
            quote = collapse_ws(document_text[start:end])
            # Quotes including a speaker header must not gain length from the role/name.
            if quote.startswith(("○", "◯", "◎")):
                quote = quote.split(turn.name, 1)[-1].strip()
            official_quotes.append(quote)
            speech_start = document_text.index(turn.name, turn.start, turn.end) + len(turn.name)
            statements.extend(
                _direct_evidence_statements(
                    document_text[speech_start : turn.end],
                    max(0, start - speech_start),
                    end - speech_start,
                )
            )
    if not official_quotes:
        return "official_evidence_missing"
    # A bare "네, 맞습니다" cannot turn the question into an executive commitment.
    if not any(len(q.replace(" ", "")) >= 12 for q in official_quotes):
        return "official_evidence_ambiguous"
    return _commitment_evidence_issue(statements, commitment)


@dataclass(frozen=True, slots=True)
class EvidenceCheck:
    quote: str
    found: bool
    score: float
    start: int | None
    end: int | None
    method: Literal["exact", "fuzzy", "missing"]


def locate_quote(source: str, quote: str, *, min_score: float = 88.0) -> EvidenceCheck:
    norm_src, index_map = normalize_with_map(source)
    norm_q = collapse_ws(quote).strip(" \"'“”‘’…")
    if len(norm_q) < 4 or not norm_src:
        return EvidenceCheck(quote, False, 0.0, None, None, "missing")
    pos = norm_src.find(norm_q)
    if pos >= 0:
        end = pos + len(norm_q) - 1
        return EvidenceCheck(quote, True, 100.0, index_map[pos], index_map[end] + 1, "exact")
    alignment = fuzz.partial_ratio_alignment(norm_q, norm_src, score_cutoff=min_score)
    if alignment is not None and alignment.dest_end > alignment.dest_start:
        start = index_map[alignment.dest_start]
        end_idx = index_map[min(alignment.dest_end, len(index_map)) - 1] + 1
        return EvidenceCheck(quote, True, round(alignment.score, 1), start, end_idx, "fuzzy")
    return EvidenceCheck(quote, False, 0.0, None, None, "missing")


@dataclass(slots=True)
class GroundingReport:
    evidence: list[EvidenceCheck]
    budget_claimed: int | None
    budget_parsed: int | None
    budget_grounded: bool | None
    year_claimed: int | None
    year_resolved: int | None
    year_grounded: bool | None
    issues: list[str] = field(default_factory=list)
    verdict: Verdict = "accepted"

    @property
    def evidence_ratio(self) -> float:
        if not self.evidence:
            return 0.0
        return sum(e.found for e in self.evidence) / len(self.evidence)

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evidence_ratio"] = round(self.evidence_ratio, 3)
        return payload


def verify_extraction(
    *,
    source: str,
    evidence_quotes: list[str],
    budget_krw: int | None,
    budget_text: str | None,
    expected_year: int | None,
    timing_text: str | None,
    reference_date: date,
    confidence: float,
    min_score: float = 88.0,
    default_unit: int = 1,
    council_document: str | None = None,
    char_start: int = 0,
    commitment: str | None = None,
) -> GroundingReport:
    checks = [locate_quote(source, q, min_score=min_score) for q in evidence_quotes]
    report = GroundingReport(
        evidence=checks,
        budget_claimed=budget_krw,
        budget_parsed=None,
        budget_grounded=None,
        year_claimed=expected_year,
        year_resolved=None,
        year_grounded=None,
    )

    found_spans = [source[c.start : c.end] for c in checks if c.found and c.start is not None]
    if not checks or not any(c.found for c in checks):
        report.issues.append("evidence_not_found")
    elif report.evidence_ratio < 1.0:
        report.issues.append("partial_evidence")

    if council_document is not None:
        issue = official_evidence_issue(
            council_document, checks, char_start=char_start, commitment=commitment
        )
        if issue:
            report.issues.append(issue)

    if budget_krw is not None:
        candidates: list[int] = []
        table = default_unit != 1  # budget-book context: bare "352,000" cells are amounts
        for span in found_spans:
            candidates += [
                a.value for a in find_amounts(span, default_unit=default_unit, bare_numbers=table)
            ]
        budget_match = (
            locate_quote(source, budget_text, min_score=min_score) if budget_text else None
        )
        if budget_match and budget_match.found:
            candidates += [
                a.value
                for a in find_amounts(
                    source[budget_match.start : budget_match.end],
                    default_unit=default_unit,
                    bare_numbers=table,
                )
            ]
        report.budget_parsed = max(candidates) if candidates else None
        report.budget_grounded = any(amounts_agree(budget_krw, c) for c in candidates)
        if not report.budget_grounded:
            report.issues.append("budget_mismatch" if candidates else "budget_unsupported")

    if expected_year is not None:
        timing_match = (
            locate_quote(source, timing_text, min_score=min_score) if timing_text else None
        )
        timing_sources = (
            [source[timing_match.start : timing_match.end]]
            if timing_match and timing_match.found
            else []
        )
        timing_sources += found_spans
        for phrase in timing_sources:
            if phrase and (t := resolve_timing(phrase, reference_date)) is not None:
                report.year_resolved = t.year
                break
        report.year_grounded = report.year_resolved == expected_year
        if not report.year_grounded:
            report.issues.append("year_unverified")

    if confidence < 0.4:
        report.issues.append("low_confidence")

    if "evidence_not_found" in report.issues:
        report.verdict = "rejected"
    elif report.issues:
        report.verdict = "needs_review"
    return report
