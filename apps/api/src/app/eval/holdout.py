"""No-network evaluation of frozen, source-grounded excerpts, with explicit label provenance.

This measures extraction on selected parsed text, not OCR, linking or forecasting. The free
heuristic is the only provider: running this command cannot spend API credits. Labels and
source text are verified before extraction. Never tune the extractor against this holdout
and continue calling it unseen data.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from app.clock import now_utc
from app.domain.krw import amounts_agree
from app.eval.realistic import Prediction, load_realistic, verify
from app.llm.prompts import ChunkContext
from app.llm.providers.heuristic import HeuristicProvider

FIELDS = ("category", "commitment", "budget_krw", "expected_year")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _inside(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("holdout path must stay inside its manifest directory")
    return path


def load_holdout(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "source-holdout-v1":
        raise ValueError("unsupported holdout schema")
    if manifest.get("frozen_before_scoring") is not True or not manifest.get("label_origin"):
        raise ValueError("explicit freeze and label provenance required")
    data = _inside(path.parent, manifest["cases_path"]).read_bytes()
    if _sha(data) != manifest["cases_sha256"]:
        raise ValueError("cases hash mismatch; do not relabel after seeing outputs")
    sources: dict[str, str] = {}
    for source in manifest["sources"]:
        if source["id"] in sources:
            raise ValueError("duplicate source ID")
        raw = _inside(path.parent, source["path"]).read_bytes()
        if _sha(raw) != source["sha256"]:
            raise ValueError(f"source hash mismatch: {source['id']}")
        sources[source["id"]] = raw.decode("utf-8")
    cases = [json.loads(line) for line in data.splitlines() if line.strip()]
    if not cases:
        raise ValueError("empty holdout")
    seen_ids: set[str] = set()
    seen_text: set[str] = set()
    development_text = {re.sub(r"\s+", "", c["text"]) for c in load_realistic()}
    for case in cases:
        if case["id"] in seen_ids:
            raise ValueError("duplicate case ID")
        seen_ids.add(case["id"])
        text = case["text"]
        normalized = re.sub(r"\s+", "", text)
        if not normalized or normalized in seen_text or normalized in development_text:
            raise ValueError("empty, duplicate or known development excerpt")
        seen_text.add(normalized)
        if text not in sources.get(case["source_id"], ""):
            raise ValueError(f"excerpt absent from archived source: {case['id']}")
        if case["doc_type"] not in {"budget_book", "council_minutes"}:
            raise ValueError("unsupported document type")
        if not case.get("date") or not case.get("date_basis"):
            raise ValueError("date and provenance required; fiscal context is not publication")
        date.fromisoformat(case["date"])
        if not isinstance(case["expected"], list):
            raise ValueError("expected must be an exhaustive signal list for the excerpt")
        for expected in case["expected"]:
            if not isinstance(expected, dict):
                raise ValueError("expected signal must be an object")
            phrases = expected.get("title_keywords")
            if (
                not isinstance(phrases, list)
                or not phrases
                or any(not isinstance(k, str) or not k.strip() for k in phrases)
            ):
                raise ValueError("nonempty predeclared title phrases list required")
            for field in ("budget_krw", "expected_year"):
                value = expected.get(field)
                if value is not None and (type(value) is not int or value < 0):
                    raise ValueError(f"{field} must be a nonnegative integer or null")
    return manifest, cases


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _normalized(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def _pairs(expected: list[dict[str, Any]], predictions: list[Prediction]) -> dict[int, int]:
    """Maximum bipartite matching, title-only, predeclared whitespace-insensitive phrases."""
    edges = {
        i: [
            j
            for j, pred in enumerate(predictions)
            if any(_normalized(k) in _normalized(pred.title) for k in item["title_keywords"])
        ]
        for i, item in enumerate(expected)
    }
    owners: dict[int, int] = {}

    def assign(i: int, visited: set[int]) -> bool:
        for j in edges[i]:
            if j in visited:
                continue
            visited.add(j)
            if j not in owners or assign(owners[j], visited):
                owners[j] = i
                return True
        return False

    for i in edges:
        assign(i, set())
    return {i: j for j, i in owners.items()}


def score_cases(rows: list[tuple[dict[str, Any], list[Prediction]]]) -> dict[str, Any]:
    expected_count = predicted_count = matched = negatives = false_negative_cases = 0
    fields = {
        k: {
            "correct": 0,
            "matched_eligible": 0,
            "all_expected_eligible": 0,
            "ambiguous_identity_excluded": 0,
        }
        for k in FIELDS
    }
    failures: list[dict[str, Any]] = []
    for case, predictions in rows:
        expected = case["expected"]
        expected_count += len(expected)
        predicted_count += len(predictions)
        negatives += not expected
        false_negative_cases += bool(not expected and predictions)
        pairs = _pairs(expected, predictions)
        matched += len(pairs)
        candidates = {
            i: {
                j
                for j, p in enumerate(predictions)
                if any(_normalized(k) in _normalized(p.title) for k in item["title_keywords"])
            }
            for i, item in enumerate(expected)
        }
        ambiguous = {
            i
            for i, js in candidates.items()
            if len(js) > 1 or any(sum(j in other for other in candidates.values()) > 1 for j in js)
        }
        for i, item in enumerate(expected):
            if i in ambiguous:
                for key in FIELDS:
                    fields[key]["ambiguous_identity_excluded"] += key in item
                failures.append({"case": case["id"], "identity_ambiguous": item["title_keywords"]})
                continue
            for key in FIELDS:
                fields[key]["all_expected_eligible"] += key in item
            if i not in pairs:
                failures.append({"case": case["id"], "missing": item["title_keywords"]})
                continue
            pred = predictions[pairs[i]]
            wrong = {}
            for key in FIELDS:
                if key not in item:  # absent means unscorable; explicit null is a label
                    continue
                fields[key]["matched_eligible"] += 1
                want, got = item[key], getattr(pred, key)
                correct = want == got
                if key == "budget_krw" and want is not None and got is not None:
                    correct = amounts_agree(want, got, tolerance=0.02)
                fields[key]["correct"] += correct
                if not correct:
                    wrong[key] = {"expected": want, "got": got}
            if wrong:
                failures.append({"case": case["id"], "title": pred.title, "wrong": wrong})
        used = set(pairs.values())
        failures.extend(
            {"case": case["id"], "unexpected": p.title}
            for i, p in enumerate(predictions)
            if i not in used
        )
    return {
        "cases": len(rows),
        "expected": expected_count,
        "predicted": predicted_count,
        "matched": matched,
        "precision": _ratio(matched, predicted_count),
        "recall": _ratio(matched, expected_count),
        "negative_cases": negatives,
        "negative_cases_with_predictions": false_negative_cases,
        "negative_case_false_positive_rate": _ratio(false_negative_cases, negatives),
        "fields": {
            key: value
            | {
                "accuracy_when_matched": _ratio(value["correct"], value["matched_eligible"]),
                "end_to_end_recall": _ratio(value["correct"], value["all_expected_eligible"]),
            }
            for key, value in fields.items()
        },
        "failures": failures,
    }


async def evaluate_holdout(path: Path, *, code_revision: str) -> dict[str, Any]:
    if not code_revision.strip():
        raise ValueError("code revision required")
    manifest, cases = await asyncio.to_thread(load_holdout, path)
    manifest_hash = _sha(await asyncio.to_thread(path.read_bytes))
    provider = HeuristicProvider()
    raw_rows, stored_rows = [], []
    predictions = []
    for case in cases:
        ctx = ChunkContext(
            case["doc_type"],
            case["id"],
            case["institution"],
            date.fromisoformat(case["date"]),
            case.get("labels", []),
            case["text"],
            case.get("fiscal_year"),
        )
        result = await provider.extract(ctx)
        raw = [Prediction.of(sig) for sig in result.value.signals]
        kept = []
        details = []
        for sig in result.value.signals:
            checked = verify(case, sig)
            details.append(
                {
                    "prediction": asdict(Prediction.of(sig, checked)),
                    "verdict": checked.report.verdict,
                    "issues": checked.report.issues,
                }
            )
            if checked.report.verdict != "rejected":
                kept.append(Prediction.of(sig, checked))
        raw_rows.append((case, raw))
        stored_rows.append((case, kept))
        predictions.append({"case": case["id"], "predictions": details})
    return {
        "schema_version": "source-holdout-result-v1",
        "evaluated_at": now_utc().isoformat(),
        "code_revision": code_revision,
        "extractor": provider.extract_model,
        "api_cost_usd": 0,
        "manifest_sha256": manifest_hash,
        "cases_sha256": manifest["cases_sha256"],
        "label_origin": manifest["label_origin"],
        "independent_human_gold": False,
        "scope": "selected_parsed_excerpts_extraction_only_not_ocr_linking_or_forecasting",
        "matching": "maximum_one_to_one_title_phrase_whitespace_insensitive_budget_2pct",
        "documents": len({case["source_id"] for case in cases}),
        "institutions": sorted({case["institution"] for case in cases}),
        "raw": score_cases(raw_rows),
        "after_verifier": score_cases(stored_rows),
        "by_institution": {
            name: score_cases(
                [(case, preds) for case, preds in stored_rows if case["institution"] == name]
            )
            for name in sorted({case["institution"] for case in cases})
        },
        "predictions": predictions,
    }
