"""Model × effort comparison on the hand-written set: quality, verifier effect, cost, latency.

``manage eval llm`` runs every case through each candidate extractor (the rule-based baseline
and one or more Claude model/effort pairs) and scores two things side by side:

* **raw** — the model's JSON as returned;
* **stored** — after the grounding verifier the pipeline runs before saving: signals whose
  evidence cannot be found in the text are dropped, amounts the parser disagrees with are
  replaced, budget lines inherit the book's fiscal year.

The gap between the two is the verifier's contribution; the cost and latency columns are what
the quality costs. Calls go straight to the provider (no LLM cache, no daily guard) so every
number is a fresh measurement, bounded by ``max_usd`` instead.
"""

from __future__ import annotations

import asyncio
import statistics
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.clock import today_kst
from app.eval.realistic import Prediction, Tally, context_for, load_realistic, verify
from app.llm.budget import MemorySpendGuard
from app.llm.prompts import EXTRACT_PROMPT_VERSION, extract_user_message
from app.llm.providers.anthropic_provider import AnthropicProvider
from app.llm.providers.heuristic import HeuristicProvider
from app.llm.schemas import EXTRACTION_SCHEMA_VERSION, ExtractedSignal
from app.llm.service import Provider, estimate_extract_cost
from app.llm.types import (
    LOCAL_QWEN_MODEL_ID,
    PRICING_PER_MTOK,
    LLMBudgetExceededError,
    LLMConfigError,
    LLMInvalidOutputError,
    LLMRefusedError,
    LLMUnavailableError,
    Usage,
)

EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_CANDIDATES = (
    "heuristic",
    "claude-opus-5:low",
    "claude-opus-5:medium",
    "claude-sonnet-5:low",
    "claude-haiku-4-5",
)
# Output-token guesses for the pre-run estimate; thinking grows with effort.
_OUTPUT_GUESS = {None: 900, "low": 900, "medium": 2000, "high": 4000, "xhigh": 6000, "max": 8000}


@dataclass(frozen=True, slots=True)
class Candidate:
    model: str
    effort: str | None = None

    @classmethod
    def parse(cls, spec: str) -> Candidate:
        """``heuristic``, ``claude-opus-5`` (model default effort) or ``claude-opus-5:low``."""
        model, _, effort = spec.strip().partition(":")
        if model != "heuristic" and model not in PRICING_PER_MTOK:
            # Costs are computed from this table; an unpriced model would report wrong numbers.
            known = ", ".join(["heuristic", *PRICING_PER_MTOK])
            raise ValueError(f"unknown model {model!r}; one of {known} (add its price first)")
        if effort and effort not in EFFORTS:
            raise ValueError(f"unknown effort {effort!r} in {spec!r}; one of {', '.join(EFFORTS)}")
        if model == LOCAL_QWEN_MODEL_ID and effort:
            raise ValueError("the pinned local model uses a fixed non-thinking profile")
        return cls(model, effort or None)

    @property
    def label(self) -> str:
        return self.model if self.effort is None else f"{self.model} · {self.effort}"

    @property
    def is_llm(self) -> bool:
        return self.model != "heuristic"


def build_provider(c: Candidate, *, api_key: str | None, max_retries: int = 4) -> Provider:
    if not c.is_llm:
        return HeuristicProvider()
    if c.model == LOCAL_QWEN_MODEL_ID:
        from app.llm.providers.local_llama import LocalLlamaProvider

        return LocalLlamaProvider()
    return AnthropicProvider(
        api_key=api_key,
        extract_model=c.model,
        extract_effort=c.effort,
        brief_model=c.model,
        brief_effort=None,
        max_retries=max_retries,
        # fallbacks="default" is documented for the models that run refusal classifiers.
        server_side_fallback=c.model.startswith(("claude-opus-5", "claude-fable-5")),
    )


@dataclass(slots=True)
class CaseRun:
    case_id: str
    signals: list[ExtractedSignal] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    served_by: str | None = None
    error: str | None = None  # refusal | invalid_output | unavailable | config | over_budget
    detail: str | None = None


def estimate(cases: list[dict[str, Any]], c: Candidate) -> Decimal:
    if not c.is_llm:
        return Decimal(0)
    out = _OUTPUT_GUESS.get(c.effort, 900)
    return sum(
        (
            estimate_extract_cost(c.model, extract_user_message(context_for(k)), output_tokens=out)
            for k in cases
        ),
        Decimal(0),
    )


async def run_candidate(
    provider: Provider,
    cases: list[dict[str, Any]],
    *,
    guard: MemorySpendGuard,
    concurrency: int = 4,
) -> list[CaseRun]:
    """One case first (writes the prompt cache), then the rest with bounded concurrency.

    The spend cap is checked before each call, so up to ``concurrency`` calls in flight can
    overshoot it by their own cost.
    """
    model = provider.extract_model
    effort = provider.extract_effort
    out_guess = _OUTPUT_GUESS.get(effort, 900)
    stop: list[str] = []

    async def one(case: dict[str, Any]) -> CaseRun:
        run = CaseRun(case["id"])
        if stop:
            run.error, run.detail = "config", "skipped after a configuration error"
            return run
        ctx = context_for(case)
        if provider.name != "heuristic":
            try:
                projected = estimate_extract_cost(
                    model, extract_user_message(ctx), output_tokens=out_guess
                )
                if projected > 0:
                    await guard.check(projected)
            except LLMBudgetExceededError as exc:
                run.error, run.detail = "over_budget", str(exc)
                return run
        try:
            result = await provider.extract(ctx)
        except LLMRefusedError as exc:
            run.error, run.detail = "refusal", str(exc)
        except LLMInvalidOutputError as exc:
            run.error, run.detail = "invalid_output", str(exc)
        except LLMUnavailableError as exc:
            run.error, run.detail = "unavailable", str(exc)
        except LLMConfigError as exc:
            run.error, run.detail = "config", str(exc)
            stop.append(str(exc))  # wrong key/model/request shape: the rest would fail the same
        else:
            run.signals = list(result.value.signals)
            run.usage = result.usage
            run.latency_ms = result.latency_ms
            run.served_by = result.served_by
            if provider.name != "heuristic":
                await guard.record(result.usage.cost_usd(model))
        return run

    if not cases:
        return []
    first = await one(cases[0])
    sem = asyncio.Semaphore(max(1, concurrency))

    async def bounded(case: dict[str, Any]) -> CaseRun:
        async with sem:
            return await one(case)

    rest = await asyncio.gather(*(bounded(k) for k in cases[1:]))
    return [first, *rest]


def summarize(
    c: Candidate, cases: list[dict[str, Any]], runs: list[CaseRun], *, min_score: float = 88.0
) -> dict[str, Any]:
    by_id = {k["id"]: k for k in cases}
    raw, stored = Tally(), Tally()
    signals = evidence_found = rejected = needs_review = budget_fixed = 0
    tokens = Usage()
    for run in runs:
        case = by_id[run.case_id]
        raw.add(case, [Prediction.of(s) for s in run.signals])
        kept: list[Prediction] = []
        for sig in run.signals:
            checked = verify(case, sig, min_score=min_score)
            signals += 1
            evidence_found += all(e.found for e in checked.report.evidence) and bool(
                checked.report.evidence
            )
            budget_fixed += checked.budget_krw != sig.budget_krw
            if checked.report.verdict == "rejected":
                rejected += 1
                continue
            needs_review += checked.report.verdict == "needs_review"
            kept.append(Prediction.of(sig, checked))
        stored.add(case, kept)
        tokens.input_tokens += run.usage.input_tokens
        tokens.output_tokens += run.usage.output_tokens
        tokens.cache_read_tokens += run.usage.cache_read_tokens
        tokens.cache_write_tokens += run.usage.cache_write_tokens

    answered = [r for r in runs if r.error is None]
    cost = tokens.cost_usd(c.model) if c.is_llm else Decimal(0)
    latencies = sorted(r.latency_ms for r in answered)
    prompt_tokens = tokens.input_tokens + tokens.cache_read_tokens + tokens.cache_write_tokens
    errors = Counter(r.error for r in runs if r.error)
    return {
        "candidate": c.label,
        "model": c.model,
        "effort": c.effort,
        "served_by": sorted({r.served_by for r in answered if r.served_by}),
        "cases": len(runs),
        "answered": len(answered),
        "errors": dict(errors),
        "error_examples": [
            {"case": r.case_id, "error": r.error, "detail": (r.detail or "")[:300]}
            for r in runs
            if r.error
        ][:5],
        "raw": raw.summary(),
        "stored": stored.summary(),
        "verifier": {
            "signals": signals,
            "evidence_found_rate": _ratio(evidence_found, signals),
            "rejected": rejected,
            "needs_review": needs_review,
            "budget_replaced_by_parser": budget_fixed,
        },
        "tokens": {
            "input": tokens.input_tokens,
            "output": tokens.output_tokens,
            "cache_read": tokens.cache_read_tokens,
            "cache_write": tokens.cache_write_tokens,
            "cache_read_share": _ratio(tokens.cache_read_tokens, prompt_tokens),
        },
        "cost_usd": float(round(cost, 4)),
        "cost_per_case_usd": float(round(cost / len(answered), 5)) if answered else None,
        "latency_ms": {
            "p50": int(statistics.median(latencies)) if latencies else None,
            "p95": latencies[max(0, round(0.95 * len(latencies)) - 1)] if latencies else None,
        },
        "failures": stored.failures[:15],
    }


async def compare(
    candidates: list[Candidate],
    *,
    api_key: str | None,
    max_usd: float,
    concurrency: int = 4,
    min_score: float = 88.0,
    cases: list[dict[str, Any]] | None = None,
    providers: dict[str, Provider] | None = None,
) -> dict[str, Any]:
    """``providers`` (label → provider) lets tests inject fakes; otherwise built from the spec."""
    cases = cases if cases is not None else load_realistic()
    guard = MemorySpendGuard(max_usd)
    results = []
    for c in candidates:
        provider = (providers or {}).get(c.label) or build_provider(c, api_key=api_key)
        runs = await run_candidate(provider, cases, guard=guard, concurrency=concurrency)
        results.append(summarize(c, cases, runs, min_score=min_score))
    return {
        "conditions": {
            "date": today_kst().isoformat(),
            "cases": len(cases),
            "expected_signals": sum(len(k["expected"]) for k in cases),
            "negative_cases": sum(not k["expected"] for k in cases),
            "prompt": EXTRACT_PROMPT_VERSION,
            "schema": EXTRACTION_SCHEMA_VERSION,
            "max_usd": max_usd,
            "spent_usd": float(round(await guard.spent_today(), 4)),
        },
        "candidates": results,
    }


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{v * 100:.1f}%"


def _usd(v: float | None) -> str:
    if v is None:
        return "–"
    return "$0" if v == 0 else (f"${v:.4f}" if v < 0.1 else f"${v:.2f}")


def render(r: dict[str, Any]) -> str:
    c = r["conditions"]
    fa = ("budget", "expected_year", "commitment", "category")
    lines = [
        "# 추출 모델 비교 (자동 생성: `manage eval llm`)",
        "",
        f"수기 세트 {c['cases']}건(기대 신호 {c['expected_signals']}건, 신호가 없어야 하는 사례 "
        f"{c['negative_cases']}건) · 프롬프트 `{c['prompt']}` · 스키마 `{c['schema']}` · "
        f"실행일 {c['date']} · 사용액 ${c['spent_usd']:.2f} (상한 ${c['max_usd']:.2f})",
        "",
        "점수는 **검증기를 거친 뒤 저장되는 값** 기준입니다. 금액·연도·확약 수준·분류는 짝지어진 신호 중 맞힌 비율이고, "
        "비용은 목록 가격으로 계산한 실제 사용량입니다.",
        "",
        "| 추출기 | 정밀도 | 재현율 | F1 | 금액 | 연도 | 확약 수준 | 분류 | 건당 비용 | 1,000건당 | 지연 p50 / p95 | 오류 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for x in r["candidates"]:
        s = x["stored"]
        per = x["cost_per_case_usd"]
        lat = x["latency_ms"]
        latency = (
            "–" if lat["p50"] is None else f"{lat['p50'] / 1000:.1f}s / {lat['p95'] / 1000:.1f}s"
        )
        if not x["model"].startswith("claude"):
            latency = "–"
        errors = ", ".join(f"{k} {v}" for k, v in x["errors"].items()) or "없음"
        lines.append(
            f"| {x['candidate']} | {_pct(s['precision'])} | {_pct(s['recall'])} | "
            f"{s['f1'] if s['f1'] is not None else '–'} | "
            + " | ".join(_pct(s["field_accuracy"][k]) for k in fa)
            + f" | {_usd(per)} | {_usd(per * 1000 if per is not None else None)} | {latency} | {errors} |"
        )
    if not any(x["model"] != "heuristic" for x in r["candidates"]):
        lines += [
            "",
            "> Claude 모델은 아직 이 표에 없습니다. API 키(`ANTHROPIC_API_KEY` 또는 "
            "`APP_ANTHROPIC_API_KEY`)를 설정하고 `make eval-llm`을 실행하면 기본 비교 대상"
            "(Opus 5 low·medium, Sonnet 5 low, Haiku 4.5)이 추가됩니다. 예상 비용은 "
            "`manage eval llm --dry-run`으로 먼저 확인합니다.",
        ]
    lines += [
        "",
        "## 검증기가 한 일",
        "",
        "| 추출기 | 원본 정밀도 → 저장 | 원본 금액 정확도 → 저장 | 근거 인용 모두 확인 | 버린 신호 | 검토 대기로 보낸 신호 | 파서가 고친 금액 | 캐시 읽기 비중 |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for x in r["candidates"]:
        raw, st, v = x["raw"], x["stored"], x["verifier"]
        lines.append(
            f"| {x['candidate']} | {_pct(raw['precision'])} → {_pct(st['precision'])} | "
            f"{_pct(raw['field_accuracy']['budget'])} → {_pct(st['field_accuracy']['budget'])} | "
            f"{_pct(v['evidence_found_rate'])} | {v['rejected']} | {v['needs_review']} | "
            f"{v['budget_replaced_by_parser']} | {_pct(x['tokens']['cache_read_share'])} |"
        )
    lines += ["", "## 틀린 사례 (추출기별 최대 15건)", ""]
    for x in r["candidates"]:
        lines.append(f"<details><summary>{x['candidate']}</summary>\n")
        for f in x["failures"]:
            if "missing" in f:
                lines.append(f"- `{f['case']}` 놓침: {f['missing']}")
            elif "unexpected" in f:
                lines.append(f"- `{f['case']}` 없는 신호를 만듦: {f['unexpected']}")
            else:
                wrong = ", ".join(
                    f"{k} {w['got']} (정답 {w['expected']})" for k, w in f["wrong"].items()
                )
                lines.append(f"- `{f['case']}` {f['title']}: {wrong}")
        for e in x["error_examples"]:
            lines.append(f"- `{e['case']}` 오류 {e['error']}: {e['detail']}")
        lines.append("\n</details>\n")
    return "\n".join(lines) + "\n"
