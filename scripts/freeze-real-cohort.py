"""Finite, free evidence run in an EMPTY local PostgreSQL database.

Read full source documents, never holdout cases/labels. Use the production
chunk/triage/extract/verify/link pipeline and freeze its actual stored outputs now.
This is a selected-source prospective cohort, not historical prediction evidence.
"""

import argparse
import asyncio
import hashlib
import json
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from app.clock import now_utc, today_kst
from app.db.models import Document, Opportunity, Signal, Source
from app.db.session import dispose_engine, session_scope
from app.demo.seed import seed_institutions
from app.eval.forecast_snapshot import export_forecast_snapshot
from app.eval.longitudinal import evaluate_longitudinal
from app.pipeline.link import link_signals
from app.pipeline.process import process_document
from app.runtime import build_runtime
from app.settings import get_settings
from sqlalchemy import func, select


def save_new(path, value):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


async def run(args):
    settings = get_settings()
    db = urlsplit(settings.database_url.replace("postgresql+asyncpg", "postgresql"))
    if not (
        settings.env == "test"
        and db.hostname in ("127.0.0.1", "localhost")
        and db.path == "/cohort"
        and settings.llm_provider == "heuristic"
        and settings.embedding_provider == "hashing"
        and settings.llm_daily_budget_usd == 0
    ):
        raise RuntimeError(
            "Requires isolated local test database /cohort and zero-cost providers"
        )
    if args.out.exists():
        raise FileExistsError(
            "Use a new output directory; never replace a frozen cohort"
        )
    runtime = build_runtime(settings)
    manifest = json.loads(args.manifest.read_text())
    # Prespecified selection uses source metadata, not labels, predictions or outcomes.
    eligible = [
        s
        for s in manifest["sources"]
        if s.get("fiscal_year", 0) >= args.min_fiscal_year
    ]
    if not eligible or len(eligible) > 20:
        raise ValueError("Expected 1..20 source documents")
    sources = []
    for item in eligible:
        path = (args.manifest.parent / item["path"]).resolve()
        if not path.is_relative_to(args.manifest.parent.resolve()):
            raise ValueError("Source path escapes manifest directory")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("Archived source hash mismatch")
        institution = runtime.registry.resolve(item["institution"]).institution
        if institution is None:
            raise ValueError("Unresolved institution")
        sources.append((item, path, raw, institution.demand_owner_code))
    args.out.mkdir(parents=True)
    counts = []
    async with session_scope() as session:
        for model in (Document, Source, Signal, Opportunity):
            if await session.scalar(select(func.count()).select_from(model)):
                raise RuntimeError("Cohort run refuses a populated database")
        await seed_institutions(session, runtime)
        source = Source(
            key="real-cohort-archive",
            name="Audited official source archive",
            adapter="archived_text",
            enabled=False,
            config={},
        )
        session.add(source)
        await session.flush()
        for item, path, raw, code in sources:
            context_date = item.get("meeting_date") or item.get("report_date")
            if not context_date:
                raise ValueError("Source requires an explicit document context date")
            doc = Document(
                source_id=source.id,
                external_id=item["id"],
                doc_type=item["doc_type"],
                title=item["title"],
                url=item["url"],
                publisher_raw=item["institution"],
                institution_code=code,
                published_at=date.fromisoformat(context_date),
                content_hash=item["sha256"],
                mime="text/plain",
                raw_uri=path.as_uri(),
                text=raw.decode("utf-8"),
                parse_status="parsed",
                parse_method="archived_text",
                structured={
                    "fiscal_year": item["fiscal_year"],
                    "published_from": "meeting_date"
                    if item.get("meeting_date")
                    else "report_date",
                    **(
                        {"meeting_date": context_date}
                        if item.get("meeting_date")
                        else {}
                    ),
                },
            )
            session.add(doc)
            await session.flush()
            result = await process_document(session, runtime, doc.id)
            counts.append(
                {
                    "source_id": item["id"],
                    "document_id": doc.id,
                    "chunks": result.chunks,
                    "triaged": result.triaged,
                    "signals": len(result.signal_ids),
                }
            )
        await session.commit()
        ids = list(await session.scalars(select(Signal.id).order_by(Signal.id)))
        if not ids:
            raise RuntimeError(
                "No stored signals; refusing to claim an initiated forecast cohort"
            )
        await link_signals(session, runtime, ids, today=today_kst())
        await session.flush()
        if not await session.scalar(select(func.count()).select_from(Opportunity)):
            raise RuntimeError(
                "No accepted forecast opportunities; cohort was not initiated"
            )
    reports = []
    for code in sorted({code for _, _, _, code in sources}):
        snapshot = args.out / f"{code}.jsonl"
        async with session_scope() as session:
            report = await export_forecast_snapshot(
                session,
                snapshot,
                institution_code=code,
                code_revision=args.code_revision,
                settings=settings,
                max_signals=3000,
            )
        observations = args.out / f"{code}-observations-initial.json"
        save_new(
            observations,
            {
                "version": "longitudinal-observations-v1",
                "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                "data_origin": "real",
                "observed_through": today_kst().isoformat(),
                "coverage": [],
                "outcomes": [],
                "note": "Initial freeze only; prior-tender screening and follow-up coverage unverified.",
            },
        )
        followup = evaluate_longitudinal(snapshot, observations, horizon_days=540)
        save_new(args.out / f"{code}-initial-report.json", followup)
        reports.append(report.to_json())
    save_new(
        args.out / "run.json",
        {
            "recorded_at": now_utc().isoformat(),
            "code_revision": args.code_revision,
            "source_manifest_sha256": hashlib.sha256(
                args.manifest.read_bytes()
            ).hexdigest(),
            "selection": {
                "min_fiscal_year": args.min_fiscal_year,
                "rule": "all full source documents meeting fiscal-year cutoff; labels never read",
            },
            "data_origin": "real",
            "api_cost_usd": 0,
            "horizon_days": 540,
            "scope": "selected archived official documents processed now; late ingestion; not historical forecasts",
            "initial_tender_screening": "not completed; later-discovered pre-freeze tenders excluded by evaluator",
            "source_documents": [item for item, _, _, _ in sources],
            "processing": counts,
            "snapshots": reports,
        },
    )
    print(
        json.dumps({"sources": len(sources), "snapshots": reports}, ensure_ascii=False)
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--min-fiscal-year", type=int, required=True)
    args = parser.parse_args()

    async def main():
        try:
            await run(args)
        finally:
            await dispose_engine()

    asyncio.run(main())
