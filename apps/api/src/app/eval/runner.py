"""Evaluation harness.

Four evaluations, each answering one question an engineer would ask before shipping a change:

* **extraction** (synthetic gold) — did we find the signals, and are budget / year / commitment /
  category / institution right? Also triage: how many chunks did we skip, and what recall did
  that cost?
* **linking** (synthetic truth) — pairwise precision/recall of "these two signals are the same
  opportunity".
* **ocr** — character error rate on scanned budget books, before vs after post-correction,
  plus exact-match rate of amount tokens (the thing a budget line is for).
* **realistic** (hand-written set) — extraction on text *not* produced by our generator: varied
  phrasing, ellipsis ("2억 8천 정도"), multiple projects per exchange, OCR-spaced budget lines.
  This is where the heuristic baseline is expected to fall short of the LLM.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any

from rapidfuzz.distance import Levenshtein
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, DocumentChunk, EvalRun, OpportunitySignal, Signal, Source
from app.domain.grounding import locate_quote
from app.domain.krw import amounts_agree
from app.eval.realistic import Prediction, Tally, context_for, load_realistic, verify
from app.runtime import Runtime
from app.sources.registry import FixtureAdapter

TEXT_STAGES = ("council_mention", "budget_line")


@dataclass(slots=True)
class _Pair:
    gold: Any
    signal: Signal | None


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


async def _docs_by_key(session: AsyncSession) -> dict[str, Document]:
    rows = (
        await session.execute(
            select(Source.key, Document).join(Document, Document.source_id == Source.id)
        )
    ).all()
    return {f"{key}:{doc.external_id}": doc for key, doc in rows}


async def eval_extraction(session: AsyncSession, world: Any) -> dict[str, Any]:
    docs = await _docs_by_key(session)
    pairs: list[_Pair] = []
    triage_hits = 0
    gold_total = 0
    matched_signal_ids: set[int] = set()
    text_doc_ids = {
        d.id for k, d in docs.items() if d.doc_type in ("council_minutes", "budget_book")
    }
    signals_by_doc: dict[int, list[Signal]] = {}
    for s in (
        await session.scalars(select(Signal).where(Signal.document_id.in_(text_doc_ids)))
    ).all():
        signals_by_doc.setdefault(s.document_id, []).append(s)
    chunks_by_doc: dict[int, list[DocumentChunk]] = {}
    for c in (
        await session.scalars(
            select(DocumentChunk).where(DocumentChunk.document_id.in_(text_doc_ids))
        )
    ).all():
        chunks_by_doc.setdefault(c.document_id, []).append(c)

    for g in world.gold:
        doc = docs.get(g.doc_key)
        if doc is None or not doc.text:
            continue
        gold_total += 1
        loc = locate_quote(doc.text, g.quote, min_score=80)
        match: Signal | None = None
        if loc.found and loc.start is not None and loc.end is not None:
            for c in chunks_by_doc.get(doc.id, []):
                if c.char_start <= loc.start < c.char_end and c.triage_passed:
                    triage_hits += 1
                    break
            best_overlap = 0
            for s in signals_by_doc.get(doc.id, []):
                if s.verdict == "rejected" or s.id in matched_signal_ids:
                    continue
                for ev in s.evidence:
                    st, en = ev.get("start"), ev.get("end")
                    if st is None or en is None:
                        continue
                    overlap = min(en, loc.end) - max(st, loc.start)
                    if overlap > best_overlap:
                        best_overlap, match = overlap, s
        if match is not None:
            matched_signal_ids.add(match.id)
        pairs.append(_Pair(g, match))

    predicted = [s for ss in signals_by_doc.values() for s in ss if s.verdict != "rejected"]
    matched = [p for p in pairs if p.signal is not None]

    def acc(field: str) -> float | None:
        ok = 0
        for p in matched:
            assert p.signal is not None
            gv, sv = getattr(p.gold, field), getattr(p.signal, field)
            if field == "budget_krw":
                ok += (gv is None and sv is None) or (
                    gv is not None and sv is not None and amounts_agree(gv, sv, tolerance=0.05)
                )
            else:
                ok += gv == sv
        return _ratio(ok, len(matched))

    chunks_total = sum(len(v) for v in chunks_by_doc.values())
    chunks_passed = sum(1 for v in chunks_by_doc.values() for c in v if c.triage_passed)
    return {
        "gold": gold_total,
        "predicted": len(predicted),
        "precision": _ratio(len(matched), len(predicted)),
        "recall": _ratio(len(matched), gold_total),
        "field_accuracy": {
            "budget": acc("budget_krw"),
            "expected_year": acc("expected_year"),
            "commitment": acc("commitment"),
            "category": acc("category"),
            "institution": acc("institution_code"),
        },
        "verdicts": {
            v: sum(1 for s in predicted if s.verdict == v) for v in ("accepted", "needs_review")
        },
        "triage": {
            "chunks": chunks_total,
            "passed": chunks_passed,
            "skipped_rate": _ratio(chunks_total - chunks_passed, chunks_total),
            "gold_recall_after_triage": _ratio(triage_hits, gold_total),
        },
    }


async def eval_linking(session: AsyncSession, world: Any) -> dict[str, Any]:
    docs = await _docs_by_key(session)
    doc_truth = {docs[k].id: v for k, v in world.doc_truth.items() if k in docs}
    rows = (
        await session.execute(
            select(Signal, OpportunitySignal.opportunity_id).join(
                OpportunitySignal, OpportunitySignal.signal_id == Signal.id
            )
        )
    ).all()
    gold_quotes = {(g.doc_key, g.truth_id): g for g in world.gold}
    key_by_doc = {d.id: k for k, d in docs.items()}
    labeled: list[tuple[str, int]] = []
    for s, opp_id in rows:
        truths = doc_truth.get(s.document_id, [])
        if len(truths) == 1:
            labeled.append((truths[0], opp_id))
        elif len(truths) > 1:
            # Multi-project document: pick the truth whose gold quote the evidence overlaps.
            doc = next(d for d in docs.values() if d.id == s.document_id)
            for tid in truths:
                g = gold_quotes.get((key_by_doc[s.document_id], tid))
                if (
                    g
                    and doc.text
                    and any(
                        g.quote[:30] in str(e.get("quote", ""))
                        or str(e.get("quote", ""))[:30] in g.quote
                        for e in s.evidence
                    )
                ):
                    labeled.append((tid, opp_id))
                    break
    tp = fp = fn = 0
    for (t1, o1), (t2, o2) in combinations(labeled, 2):
        same_truth, same_opp = t1 == t2, o1 == o2
        tp += same_truth and same_opp
        fp += (not same_truth) and same_opp
        fn += same_truth and not same_opp
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    f1 = round(2 * precision * recall / (precision + recall), 3) if precision and recall else None
    truth_to_opps: dict[str, set[int]] = {}
    opp_to_truths: dict[int, set[str]] = {}
    for t, o in labeled:
        truth_to_opps.setdefault(t, set()).add(o)
        opp_to_truths.setdefault(o, set()).add(t)
    return {
        "labeled_signals": len(labeled),
        "pairwise": {"precision": precision, "recall": recall, "f1": f1},
        "truths_in_single_opportunity": _ratio(
            sum(len(v) == 1 for v in truth_to_opps.values()), len(truth_to_opps)
        ),
        "pure_opportunities": _ratio(
            sum(len(v) == 1 for v in opp_to_truths.values()), len(opp_to_truths)
        ),
    }


def _cer(hyp: str, ref: str) -> float:
    h = " ".join(hyp.split())
    r = " ".join(ref.split())
    return Levenshtein.distance(h, r) / max(len(r), 1)


_AMOUNT_TOKEN = re.compile(r"\d{1,3}(?:,\d{3})+")


async def eval_ocr(world: Any, runtime: Runtime, limit: int = 12) -> dict[str, Any]:
    if runtime.ocr is None:
        return {"skipped": "tesseract not available"}
    import pypdfium2 as pdfium
    from pypdf import PdfReader

    from app.parsing.ocr_correct import correct_ocr_text

    raw_cers: list[float] = []
    fixed_cers: list[float] = []
    amounts_total = amounts_raw = amounts_fixed = 0
    for rec in world.records["fixture_budget"]:
        if rec.mime != "application/pdf" or not rec.content:
            continue
        import io

        if (PdfReader(io.BytesIO(rec.content)).pages[0].extract_text() or "").strip():
            continue  # has a text layer; not a scan
        ref = world.source_texts[f"fixture_budget:{rec.external_id}"]
        pdf = pdfium.PdfDocument(rec.content)
        raw_pages = []
        for page in pdf:
            raw_pages.append(
                (await runtime.ocr.recognize(page.render(scale=300 / 72).to_pil())).text
            )
        pdf.close()
        raw = "\n".join(raw_pages)
        fixed = correct_ocr_text(raw, runtime.corrector)
        raw_cers.append(_cer(raw, ref))
        fixed_cers.append(_cer(fixed, ref))
        ref_amounts = _AMOUNT_TOKEN.findall(ref)
        amounts_total += len(ref_amounts)
        amounts_raw += sum(1 for a in ref_amounts if a in raw)
        amounts_fixed += sum(1 for a in ref_amounts if a in fixed)
        if len(raw_cers) >= limit:
            break
    n = len(raw_cers)
    return {
        "scanned_documents": n,
        "cer_raw": round(sum(raw_cers) / n, 4) if n else None,
        "cer_corrected": round(sum(fixed_cers) / n, 4) if n else None,
        "amount_token_accuracy_raw": _ratio(amounts_raw, amounts_total),
        "amount_token_accuracy_corrected": _ratio(amounts_fixed, amounts_total),
    }


async def eval_realistic(session: AsyncSession, runtime: Runtime) -> dict[str, Any]:
    cases = load_realistic()
    raw, stored = Tally(), Tally()
    rejected = 0
    for case in cases:
        attempt = await runtime.llm.extract(session, context_for(case))
        raw.add(case, [Prediction.of(s) for s in attempt.output.signals])
        kept: list[Prediction] = []
        for sig in attempt.output.signals:
            checked = verify(case, sig, min_score=runtime.settings.grounding_min_score)
            if checked.report.verdict == "rejected":
                rejected += 1
            else:
                kept.append(Prediction.of(sig, checked))
        stored.add(case, kept)
    return {
        "cases": len(cases),
        "extractor": (runtime.llm.primary or runtime.llm.fallback).extract_model,
        **raw.summary(),
        "after_verifier": stored.summary() | {"rejected": rejected},
        "failures": raw.failures[:20],
    }


async def run_all_evals(
    session: AsyncSession, runtime: Runtime, *, record: bool, report_path: Path | None
) -> dict[str, Any]:
    src = await session.scalar(select(Source).where(Source.key == "fixture_minutes"))
    if src is None:
        raise RuntimeError("seed the demo world first (manage seed && manage demo run)")
    world = FixtureAdapter(src.key, src.config).world()
    results: dict[str, Any] = {
        "conditions": {
            "anchor": src.config.get("anchor"),
            "seed": src.config.get("seed"),
            "scale": src.config.get("scale"),
            "scanned_ratio": src.config.get("scanned_ratio"),
            "extractor_mode": runtime.extractor_mode,
        },
        "extraction": await eval_extraction(session, world),
        "linking": await eval_linking(session, world),
        "ocr": await eval_ocr(world, runtime),
        "realistic": await eval_realistic(session, runtime),
    }
    backtest = await session.scalar(
        select(EvalRun)
        .where(EvalRun.kind == "backtest")
        .order_by(EvalRun.created_at.desc())
        .limit(1)
    )
    if backtest is not None:
        results["backtest"] = backtest.metrics
    if record:
        params = {
            "anchor": src.config.get("anchor"),
            "seed": src.config.get("seed"),
            "extractor_mode": runtime.extractor_mode,
        }
        for kind, key in (
            ("extraction", "extraction"),
            ("ranking", "linking"),
            ("ocr", "ocr"),
            ("extraction", "realistic"),
        ):
            session.add(EvalRun(kind=kind, label=key, metrics=results[key], params=params))
    if report_path is not None:
        await asyncio.to_thread(report_path.write_text, render_report(results), encoding="utf-8")
    return results


def _pct(v: Any) -> str:
    return "–" if v is None else f"{v * 100:.1f}%"


def render_report(r: dict[str, Any]) -> str:
    e, lk, o, rl = r["extraction"], r["linking"], r["ocr"], r["realistic"]
    av = rl["after_verifier"]
    bt = r.get("backtest", {})
    c = r.get("conditions", {})
    lines = [
        "# 평가 결과 (자동 생성: `manage eval all --report`)",
        "",
        "> 합성 세계(synthetic world) 결과는 파이프라인이 설계대로 동작하는지 보여줄 뿐, 실제 데이터에서의",
        "> 정확도를 주장하지 않습니다. 실제 문장에 가까운 수기 작성 세트(realistic)를 따로 둔 이유입니다.",
        "",
        f"조건: 기준일 {c.get('anchor')} · 시드 {c.get('seed')} · 규모 {c.get('scale')} · "
        f"스캔 비율 {'기본값' if c.get('scanned_ratio') is None else c['scanned_ratio']} · "
        f"추출기 `{c.get('extractor_mode')}`",
        "",
        "## 추출 (합성 정답 대비)",
        f"- 정밀도 {_pct(e['precision'])} · 재현율 {_pct(e['recall'])} (정답 {e['gold']}건, 예측 {e['predicted']}건; 검토 대기 포함)",
        f"- 자동 승인 {e['verdicts']['accepted']}건 · 검토 대기 {e['verdicts']['needs_review']}건. 저장 재현율은 자동 승인 재현율이 아닙니다.",
        "- 필드 정확도: " + ", ".join(f"{k} {_pct(v)}" for k, v in e["field_accuracy"].items()),
        f"- 트리아지: 청크 {e['triage']['chunks']}개 중 {_pct(e['triage']['skipped_rate'])}를 LLM 호출 없이 건너뜀, "
        f"그 상태에서 정답 신호 재현율 {_pct(e['triage']['gold_recall_after_triage'])}",
        "",
        "## 기회 연결 (linking)",
        f"- 쌍(pairwise) 정밀도 {_pct(lk['pairwise']['precision'])} · 재현율 {_pct(lk['pairwise']['recall'])} · F1 {lk['pairwise']['f1']}",
        f"- 한 기회로 온전히 묶인 실제 사업 비율 {_pct(lk['truths_in_single_opportunity'])}, 순수한 기회 비율 {_pct(lk['pure_opportunities'])}",
        "",
        "## OCR (스캔 예산서)",
        f"- 문서 {o.get('scanned_documents')}건 · CER {o.get('cer_raw')} → 보정 후 {o.get('cer_corrected')}",
        f"- 금액 토큰 정확도 {_pct(o.get('amount_token_accuracy_raw'))} → {_pct(o.get('amount_token_accuracy_corrected'))}",
        "",
        f"## 수기 작성 세트 (extractor: {rl['extractor']}, {rl['cases']}건)",
        f"- 정밀도 {_pct(rl['precision'])} · 재현율 {_pct(rl['recall'])} (기대 {rl['expected']}건, 예측 {rl['predicted']}건)",
        "- 필드 정확도: " + ", ".join(f"{k} {_pct(v)}" for k, v in rl["field_accuracy"].items()),
        f"- 검증기 통과 후(저장되는 값): 정밀도 {_pct(av['precision'])} · 재현율 {_pct(av['recall'])} · "
        + ", ".join(f"{k} {_pct(v)}" for k, v in av["field_accuracy"].items())
        + f" · 근거를 찾지 못해 버린 신호 {av['rejected']}건",
        "- 모델별 비교는 `manage eval llm` → [evaluation-llm.md](evaluation-llm.md)",
        "",
    ]
    if bt:
        cov = bt.get("tender_early_coverage", {})
        lead = bt.get("lead_time_days", {})
        lines += [
            "## 백테스트 (현재 연결의 회고적 진단)",
            f"- 방법: `{bt.get('method_version', 'legacy')}` · 기준일 {bt.get('as_of')} · 관측기간 {bt.get('horizon_days')}일",
            f"- 관측기간 미충족 기회 {bt.get('censored_opportunities', 0)}건 · 공개일 불확실 제외 신호 {bt.get('excluded_uncertain_dates', 0)}건",
            "- 합성 데이터 실행 수치는 실제 예측 성능이 아닙니다. 현재 연결을 사용하므로 과거 시점 예측 재현도 아닙니다.",
            f"- 입찰공고 {cov.get('tenders')}건 중 {_pct(cov.get('rate'))}가 공고 이전에 공개 신호를 가짐",
            f"- 선행 기간 중앙값 {lead.get('median')}일 (p25 {lead.get('p25')}, p75 {lead.get('p75')})",
            "- 첫 신호 유형별 입찰 전환율:",
        ]
        for k, v in bt.get("conversion_by_first_signal", {}).items():
            lines.append(f"  - `{k}` n={v['n']} → {_pct(v['rate'])}")
    return "\n".join(lines) + "\n"
