"""Cheap pre-filter in front of the LLM.

A two-hour committee transcript is mostly procedure, statistics and complaints. Sending every
exchange to the model would multiply cost for no recall. Triage scores each chunk with a
transparent linear model over lexical features and forwards only chunks above the threshold.

The eval (``manage eval extraction``) reports both sides of the trade-off: the share of chunks
dropped (≈ tokens saved) and the recall of gold signals that survive triage.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from app.domain.krw import find_amounts
from app.domain.taxonomy import (
    CATEGORIES,
    COMMITMENT_LADDER,
    NOISE_MARKERS,
    PROCUREMENT_VERBS,
)

_FUTURE_MARKERS = ("내년", "내후년", "예정", "계획", "하반기", "상반기", "본예산", "추경", "년도")
_KEYWORDS = tuple({kw for info in CATEGORIES.values() for kw in info.keywords})
_COMMITMENT = (
    *(p for phrases in COMMITMENT_LADDER.values() for p in phrases),
    "계상하였",
    "계상했",
    "계상하겠",
)

# Weights hand-set, then checked against the synthetic gold set; see docs/evaluation.md.
_WEIGHTS = {
    "bias": -2.4,
    "procurement_verbs": 0.55,
    "category_keywords": 0.6,
    "commitment": 1.3,
    "amount": 1.1,
    "future": 0.45,
    "noise": -0.9,
    "budget_line": 1.6,
}


@dataclass(frozen=True, slots=True)
class TriageResult:
    score: float
    passed: bool
    features: dict[str, float]


def _count(text: str, words: tuple[str, ...], cap: int = 3) -> float:
    return float(min(sum(1 for w in words if w in text), cap))


def triage_chunk(text: str, *, kind: str, threshold: float) -> TriageResult:
    if kind == "record":
        return TriageResult(1.0, True, {"record": 1.0})
    if kind == "procedure":
        return TriageResult(0.0, False, {"procedure": 1.0})
    features = {
        "procurement_verbs": _count(text, PROCUREMENT_VERBS),
        "category_keywords": _count(text, _KEYWORDS),
        "commitment": _count(text, _COMMITMENT, cap=2),
        "amount": 1.0 if find_amounts(text) or re.search(r"\d{1,3}(?:,\d{3}){1,}", text) else 0.0,
        "future": _count(text, _FUTURE_MARKERS, cap=2),
        "noise": _count(text, NOISE_MARKERS, cap=2),
        "budget_line": 1.0 if kind in ("budget_line", "budget_plan", "budget_review") else 0.0,
    }
    z = _WEIGHTS["bias"] + sum(_WEIGHTS[k] * v for k, v in features.items())
    score = 1 / (1 + math.exp(-z))
    return TriageResult(round(score, 4), score >= threshold, features)
