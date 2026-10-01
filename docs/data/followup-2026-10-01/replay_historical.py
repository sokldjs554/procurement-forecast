"""Reproduce the parsed-budget replay without any tender/outcome input.

From apps/api: uv run python ../../docs/data/followup-2026-10-01/replay_historical.py
  --revision <code SHA> --out /tmp/new-replay.json
"""

import argparse
import asyncio
import hashlib
import json
from datetime import date
from pathlib import Path

from app.clock import now_utc
from app.domain.krw import detect_table_unit
from app.llm.prompts import ChunkContext
from app.llm.providers.heuristic import HeuristicProvider
from app.parsing.chunking import chunk_budget
from app.pipeline.process import check_signal
from app.pipeline.triage import triage_chunk


async def replay(revision: str) -> dict:
    root = Path(__file__).resolve().parents[3]
    source = root / "docs/data/blind-2026-09-30/v2-adjudicated-20260930/sources/gangnam_budget_2025.txt"
    text = source.read_text()
    provider = HeuristicProvider()
    chunks = chunk_budget(text)
    rows = []
    passed = 0
    for chunk in chunks:
        if not triage_chunk(chunk.text, kind=chunk.kind, threshold=0.35).passed:
            continue
        passed += 1
        ctx = ChunkContext(
            "budget_book", "2025년도 예산안 검토보고서", "서울특별시 강남구",
            date(2024, 12, 17), chunk.labels, chunk.text, 2025,
        )
        for signal in (await provider.extract(ctx)).value.signals:
            checked = check_signal(
                signal, text=chunk.text, doc_type="budget_book",
                reference_date=date(2024, 12, 17), fiscal_year=2025,
                table_unit=detect_table_unit(chunk.text) or 1000, min_score=88,
            )
            rows.append({
                "chunk_start": chunk.char_start, "chunk_end": chunk.char_end,
                "signal": signal.model_dump(mode="json"), "verdict": checked.report.verdict,
                "stored_budget_krw": checked.budget_krw,
                "stored_expected_year": checked.expected_year,
            })
    return {
        "executed_at": now_utc().isoformat(), "code_revision": revision,
        "extractor": provider.extract_model, "source_path": str(source.relative_to(root)),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "input_report_date": "2024-12-17", "public_availability_date": None,
        "method": "offline_full_parsed_document_replay_with_production_chunking_triage_extraction_verifier",
        "future_outcome_data_passed_to_extractor": False,
        "limitations": [
            "Current code replay on archived old document, not an actual prediction made in 2024.",
            "Report date does not prove public availability; no verified lead-time claim.",
            "No OCR, DB linking or complete outcome coverage tested.",
        ],
        "chunks": len(chunks), "triage_passed": passed, "signals": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    # Refuse overwriting any previous observations or frozen output.
    with args.out.open("x") as output:
        json.dump(asyncio.run(replay(args.revision)), output, ensure_ascii=False, indent=2)
