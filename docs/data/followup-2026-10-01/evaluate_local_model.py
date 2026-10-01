#!/usr/bin/env python3
"""Zero-API-cost exploratory evaluation; run with a loopback llama.cpp server.

Uses the unchanged extract-v3 prompt, frozen source loader, title matcher and production
grounding. Annotation fields never enter the model request. All cases remain in denominators,
including request/validation failures. Saves every request/response before scoring.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path

import httpx

from app.clock import now_utc
from app.domain.krw import detect_table_unit
from app.eval.holdout import load_holdout, score_cases
from app.eval.realistic import Prediction
from app.llm.prompts import EXTRACT_PROMPT_VERSION, EXTRACT_SYSTEM, ChunkContext, extract_user_message
from app.llm.schemas import EXTRACTION_SCHEMA_VERSION, ExtractionOutput, strict_json_schema
from app.pipeline.process import check_signal

MODEL = "Qwen3-4B-Q4_K_M"
MODEL_SHA256 = "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5"
MODEL_REVISION = "bc640142c66e1fdd12af0bd68f40445458f3869b"
RUNTIME_REVISION = "feb9a3d6debb3a8544052b04c84fa1f445fd77f5"
SAMPLING = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0,
            "presence_penalty": 1.5, "seed": 42, "max_tokens": 4096}


def write_new(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


async def run(args: argparse.Namespace) -> None:
    manifest, cases = load_holdout(args.manifest)
    args.output.mkdir(parents=True, exist_ok=False)
    schema = strict_json_schema(ExtractionOutput)
    metadata = {
        "schema_version": "source-local-model-evaluation-v1",
        "started_at": now_utc().isoformat(), "code_revision": args.code_revision,
        "model": MODEL, "model_revision": MODEL_REVISION, "model_sha256": MODEL_SHA256,
        "runtime_revision": RUNTIME_REVISION, "prompt_version": EXTRACT_PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(EXTRACT_SYSTEM.encode()).hexdigest(),
        "schema_version_extraction": EXTRACTION_SCHEMA_VERSION,
        "schema_sha256": hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest(),
        "sampling": SAMPLING, "thinking": False, "context_tokens": 8192,
        "hardware": "CPU only; 6 threads; no paid API", "api_cost_usd": 0,
        "cost_excludes": "host hardware and electricity",
        "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        "cases_sha256": manifest["cases_sha256"], "label_origin": manifest["label_origin"],
        "independent_human_gold": False,
        "scope": "selected_parsed_excerpts_extraction_only_not_ocr_linking_or_forecasting",
        "context_policy": "generic title; verified source text, institution, date, and speaker labels only",
        "matching": "unchanged maximum_one_to_one_title_phrase_whitespace_insensitive_budget_2pct",
    }
    write_new(args.output / "run-frozen.json", metadata)
    raw_rows, kept_rows, predictions, errors, latencies = [], [], [], [], []
    input_tokens = output_tokens = 0
    async with httpx.AsyncClient(timeout=480, trust_env=False) as client:
        for index, case in enumerate(cases, 1):
            ctx = ChunkContext(case["doc_type"], "공식 문서 발췌", case["institution"],
                               date.fromisoformat(case["date"]), case["_source_labels"],
                               case["text"], case.get("fiscal_year"))
            request = {
                "model": MODEL,
                "messages": [{"role": "system", "content": EXTRACT_SYSTEM},
                             {"role": "user", "content": extract_user_message(ctx)}],
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "extraction", "strict": True, "schema": schema}},
                "chat_template_kwargs": {"enable_thinking": False},
                "reasoning_effort": "none", "stream": False, **SAMPLING,
            }
            write_new(args.output / f"{index:02d}-request.json", request)
            started = time.perf_counter()
            raw, kept, details = [], [], []
            try:
                response = await client.post("http://127.0.0.1:18080/v1/chat/completions", json=request)
                response.raise_for_status()
                body = response.json()
                write_new(args.output / f"{index:02d}-response.json", body)
                choice = body["choices"][0]
                if choice["finish_reason"] != "stop":
                    raise ValueError(f"incomplete generation: {choice['finish_reason']}")
                parsed = ExtractionOutput.model_validate_json(choice["message"]["content"])
                usage = body.get("usage", {})
                input_tokens += usage.get("prompt_tokens", 0)
                output_tokens += usage.get("completion_tokens", 0)
                raw = [Prediction.of(sig) for sig in parsed.signals]
                for sig in parsed.signals:
                    checked = check_signal(
                        sig, text=case["text"], doc_type=case["doc_type"],
                        reference_date=date.fromisoformat(case["date"]),
                        fiscal_year=case.get("fiscal_year"),
                        table_unit=detect_table_unit(case["text"]) or 1000, min_score=88.0,
                        document_text=case["_source_text"], char_start=case["_char_start"],
                    )
                    details.append({"prediction": asdict(Prediction.of(sig, checked)),
                                    "verdict": checked.report.verdict, "issues": checked.report.issues})
                    if checked.report.verdict != "rejected":
                        kept.append(Prediction.of(sig, checked))
            except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
                errors.append({"case": case["id"], "type": type(exc).__name__, "error": str(exc)})
                write_new(args.output / f"{index:02d}-error.json", errors[-1])
            latency = round(time.perf_counter() - started, 3)
            latencies.append(latency)
            raw_rows.append((case, raw))
            kept_rows.append((case, kept))
            predictions.append({"case": case["id"], "latency_seconds": latency, "predictions": details})
            print(json.dumps({"completed": index, "total": len(cases), "raw": len(raw),
                              "kept": len(kept), "seconds": latency, "errors": len(errors)}), flush=True)
    result = {
        **metadata, "finished_at": now_utc().isoformat(),
        "documents": len({c["source_id"] for c in cases}),
        "institutions": sorted({c["institution"] for c in cases}),
        "raw": score_cases(raw_rows), "after_verifier": score_cases(kept_rows),
        "errors": errors, "predictions": predictions,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        "latency_seconds": {"total": round(sum(latencies), 3), "median": statistics.median(latencies)},
    }
    write_new(args.output / "result.json", result)
    print(json.dumps({"result": str(args.output / "result.json"),
                      "after_verifier": result["after_verifier"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--code-revision", required=True)
    asyncio.run(run(parser.parse_args()))
