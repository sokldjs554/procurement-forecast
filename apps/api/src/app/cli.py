"""``manage`` command line.

manage db upgrade                 # alembic upgrade head
manage seed [--anchor 2026-09-25] # institutions, sources, demo tenants
manage demo run                   # full pipeline over the synthetic world, in-process
manage eval all --record          # extraction / linking / OCR / realistic-set evals
manage eval llm --dry-run         # Claude model × effort comparison on the hand-written set
manage bench --report ../../docs/performance.md   # hot-query plans at volume
manage sources check              # first real call to each 조달청 operation (needs the data.go.kr key)
manage sources check -s clik_minutes   # CLIK minutes list + one detail (needs the CLIK key)
manage sources ingest -s g2b --days 30 --max-calls 250   # backfill a window, counting calls
manage pipeline run               # process pending documents and link their signals
manage media transcribe VIDEO     # council meeting video → transcript (checkpointed, resumable)
manage media ingest VIDEO --title … --meeting-date …   # … → document → signals with video times
manage queue enqueue-media VIDEO --org 1 --title … --meeting-date …   # the same, as a queued job
manage queue worker               # Postgres-queue stage worker (media.transcribe)
manage apikey create --org 1 --name ci --scope jobs:write   # printed once
manage worker                     # arq worker + cron (+ /healthz on $PORT for Cloud Run)
manage ops bootstrap              # real reference data only (institutions, keyed live sources)
manage ops tick                   # one scheduled pass without a resident worker or Redis
manage openapi > openapi.json     # schema for the web app's generated types
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Awaitable, Callable, Coroutine
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

import typer

from app.clock import today_kst
from app.log import configure_logging
from app.settings import get_settings

app = typer.Typer(help="발주 예측 operations CLI", no_args_is_help=True)
db_app = typer.Typer(help="Database")
demo_app = typer.Typer(help="Synthetic demo world")
eval_app = typer.Typer(help="Evaluations")
sources_app = typer.Typer(help="External data sources")
pipeline_app = typer.Typer(help="Pipeline stages outside the worker")
llm_cache_app = typer.Typer(help="Paid LLM answers, kept across databases")
link_app = typer.Typer(help="Linking real signals again, without fetching or extracting")
media_app = typer.Typer(help="Council meeting video → transcript → signals")
queue_app = typer.Typer(help="Postgres job queue (long, costed, tenant-owned jobs)")
apikey_app = typer.Typer(help="API keys for the job API")
ops_app = typer.Typer(help="Scheduled operation without a resident worker (free hosting)")
app.add_typer(db_app, name="db")
app.add_typer(demo_app, name="demo")
app.add_typer(eval_app, name="eval")
app.add_typer(sources_app, name="sources")
app.add_typer(pipeline_app, name="pipeline")
app.add_typer(llm_cache_app, name="llm-cache")
app.add_typer(link_app, name="link")
app.add_typer(media_app, name="media")
app.add_typer(queue_app, name="queue")
app.add_typer(apikey_app, name="apikey")
app.add_typer(ops_app, name="ops")

T = TypeVar("T")


def _run(fn: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(fn())


async def _with_session(fn: Callable[..., Awaitable[T]], **kwargs: Any) -> T:
    from redis.asyncio import Redis

    from app.db.session import dispose_engine, session_scope
    from app.runtime import build_runtime

    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    runtime = build_runtime(settings, redis=redis)
    try:
        async with session_scope() as session:
            return await fn(session, runtime, **kwargs)
    finally:
        await redis.aclose()
        await dispose_engine()


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    """Apply migrations (Cloud Run Job runs this before each deploy)."""
    root = Path(__file__).resolve().parents[2]
    subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "-c", str(root / "alembic.ini"), "upgrade", revision],
        check=True,
        cwd=root,
    )


@app.command()
def seed(
    anchor: str = typer.Option(None, help="Synthetic world 'today' (YYYY-MM-DD)"),
    seed_value: int = typer.Option(7, "--seed"),
    scale: float = 1.0,
) -> None:
    """Seed institutions, sources and demo tenants."""
    configure_logging(json=False)
    anchor_date = date.fromisoformat(anchor) if anchor else today_kst()

    async def go(session: Any, runtime: Any) -> None:
        from app.demo.seed import seed_institutions, seed_sources, seed_tenants

        n = await seed_institutions(session, runtime)
        await seed_sources(session, runtime, anchor=anchor_date, seed=seed_value, scale=scale)
        await session.flush()
        orgs = await seed_tenants(session, runtime)
        typer.echo(f"institutions={n} demo_orgs={orgs} anchor={anchor_date}")

    _run(lambda: _with_session(go))


@demo_app.command("snapshot")
def demo_snapshot(
    out: Path = typer.Argument(..., help="Directory the static demo reads (apps/web/public/demo)"),
    base_url: str = typer.Option("http://127.0.0.1:8000", help="A running API over the demo world"),
) -> None:
    """Record the API's answers for the static public demo (see app/demo/snapshot.py). Makes a
    brief per opportunity in the demo company's feed, so run it on a throwaway database."""
    configure_logging(json=False, level="WARNING", stream=sys.stderr)

    async def go() -> dict[str, int]:
        from app.db.session import dispose_engine
        from app.demo.snapshot import record_snapshot

        try:
            return await record_snapshot(base_url, out)
        finally:
            await dispose_engine()

    typer.echo(json.dumps(_run(go), ensure_ascii=False))


@demo_app.command("run")
def demo_run(anchor: str = typer.Option(None)) -> None:
    """Ingest the synthetic world and run every pipeline stage in-process."""
    configure_logging(json=False)

    async def go(session: Any, runtime: Any) -> None:
        from sqlalchemy import select

        from app.db.models import Source
        from app.demo.seed import run_demo_pipeline

        src = await session.scalar(select(Source).where(Source.key == "fixture_minutes"))
        if src is None:
            raise typer.BadParameter("run `manage seed` first")
        anchor_date = date.fromisoformat(anchor or src.config["anchor"])
        report = await run_demo_pipeline(session, runtime, anchor=anchor_date)
        typer.echo(
            json.dumps(
                {
                    "documents": report.documents,
                    "signals": report.signals,
                    "opportunities": report.opportunities,
                    "needs_review": report.needs_review,
                    "degraded": report.degraded,
                    "tender_early_coverage": report.backtest.get("tender_early_coverage"),
                    "lead_time_days": report.backtest.get("lead_time_days", {}).get("median"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    _run(lambda: _with_session(go))


@eval_app.command("all")
def eval_all(
    record: bool = typer.Option(False, help="Store results as eval_runs"),
    report: Path = typer.Option(None, help="Write a Markdown report here"),
) -> None:
    """Extraction, triage, linking and OCR metrics against the synthetic ground truth, plus the
    hand-written realistic set."""
    configure_logging(json=False, level="WARNING", stream=sys.stderr)

    async def go(session: Any, runtime: Any) -> dict[str, Any]:
        from app.eval.runner import run_all_evals

        return await run_all_evals(session, runtime, record=record, report_path=report)

    results = _run(lambda: _with_session(go))
    typer.echo(json.dumps(results, ensure_ascii=False, indent=2, default=str))


@eval_app.command("llm")
def eval_llm(
    model: list[str] = typer.Option(
        None,
        "--model",
        "-m",
        help="Repeatable: heuristic | <model> | <model>:<effort>. Default: "
        "heuristic, claude-opus-5:low, claude-opus-5:medium, claude-sonnet-5:low, "
        "claude-haiku-4-5",
    ),
    max_usd: float = typer.Option(10.0, help="Stop calling the API once this much is spent"),
    concurrency: int = typer.Option(4, help="Calls in flight per model"),
    dry_run: bool = typer.Option(False, help="Print the estimated cost and exit"),
    report: Path = typer.Option(None, help="Write a Markdown report here"),
    record: bool = typer.Option(False, help="Also store each model as an eval_runs row"),
) -> None:
    """Run the hand-written set through each extractor: quality after the grounding verifier,
    cost, latency. Reads the key from APP_ANTHROPIC_API_KEY or ANTHROPIC_API_KEY."""
    from app.eval.llm_compare import DEFAULT_CANDIDATES, Candidate, compare, estimate, render
    from app.eval.realistic import load_realistic

    configure_logging(json=False, level="WARNING", stream=sys.stderr)
    try:
        candidates = [Candidate.parse(m) for m in (model or DEFAULT_CANDIDATES)]
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    cases = load_realistic()
    total = 0.0
    for c in candidates:
        est = float(estimate(cases, c))
        total += est
        typer.echo(f"{c.label:<28} {len(cases)} cases  ~${est:.2f}", err=True)
    typer.echo(f"{'estimated total':<28} ~${total:.2f} (cap ${max_usd:.2f})", err=True)
    if total > max_usd:
        typer.echo(
            "warning: estimate exceeds --max-usd; calls stop once the cap is spent", err=True
        )
    if dry_run:
        return

    settings = get_settings()
    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    results = _run(
        lambda: compare(
            candidates,
            api_key=key,
            max_usd=max_usd,
            concurrency=concurrency,
            min_score=settings.grounding_min_score,
            cases=cases,
        )
    )
    if report is not None:
        report.write_text(render(results), encoding="utf-8")
    if record:

        async def store(session: Any, _runtime: Any) -> None:
            from app.db.models import EvalRun

            for x in results["candidates"]:
                session.add(
                    EvalRun(
                        kind="extraction",
                        label=f"llm-compare:{x['candidate']}"[:100],
                        metrics=x,
                        params=results["conditions"],
                    )
                )

        _run(lambda: _with_session(store))
    typer.echo(json.dumps(results, ensure_ascii=False, indent=2, default=str))


@sources_app.command("check")
def sources_check(
    source: list[str] = typer.Option(
        None,
        "--source",
        "-s",
        help="g2b_order_plan | g2b_prespec | g2b_bid | clik_minutes (default: the three g2b)",
    ),
    days: int = typer.Option(7, min=1, max=31, help="Look back this many days (g2b)"),
    rows: int = typer.Option(20, min=1, max=100, help="Items per call"),
    council: str = typer.Option(None, help="CLIK 지방의회 ID (rasmblyId), e.g. 031013"),
    report: Path = typer.Option(None, help="Write a Markdown report here"),
) -> None:
    """Call each 조달청 operation once with APP_DATA_GO_KR_SERVICE_KEY — or, with
    ``-s clik_minutes``, the CLIK minutes list and one detail with APP_CLIK_API_KEY — and check
    that the adapter can read what comes back. No database needed; nothing is stored.

    Exits 1 when any call failed or an operation's items could not be mapped."""
    from app.sources.check import as_json, check_clik, check_g2b, problems, render
    from app.sources.g2b import OPERATIONS

    configure_logging(json=False, level="WARNING", stream=sys.stderr)
    known = [*OPERATIONS, "clik_minutes"]
    if unknown := sorted(set(source or ()) - set(known)):
        raise typer.BadParameter(
            f"unknown {', '.join(unknown)}; one of {', '.join(known)}", param_hint="--source"
        )
    wanted = list(source or OPERATIONS)
    g2b_sources = [s for s in wanted if s in OPERATIONS]
    settings = get_settings()
    checks = []
    if "clik_minutes" in wanted:
        clik_secret = settings.clik_api_key
        if clik_secret is None:
            typer.echo("APP_CLIK_API_KEY is not set (.env or environment)", err=True)
            raise typer.Exit(2)
        clik_key = clik_secret.get_secret_value()
        checks += _run(lambda: check_clik(clik_key, rows=min(rows, 100), council=council))
    if g2b_sources:
        secret = settings.data_go_kr_service_key
        if secret is None:
            typer.echo("APP_DATA_GO_KR_SERVICE_KEY is not set (.env or environment)", err=True)
            raise typer.Exit(2)
        key = secret.get_secret_value()
        checks += _run(lambda: check_g2b(key, sources=g2b_sources, days=days, rows=rows))
    for c in checks:
        status = f"{c.mapped}/{c.items} mapped (total {c.total})" if c.ok else f"FAILED {c.error}"
        typer.echo(f"{c.path.rsplit('/', 1)[-1]:<36} {status}", err=True)
    if report is not None:
        report.write_text(render(checks, days=days), encoding="utf-8")
    typer.echo(json.dumps(as_json(checks), ensure_ascii=False, indent=2, default=str))
    if issues := problems(checks):
        typer.echo(f"{len(issues)} problem(s); see the report", err=True)
        raise typer.Exit(1)


@sources_app.command("ingest")
def sources_ingest(
    source: list[str] = typer.Option(
        ..., "--source", "-s", help="Source key, or an adapter name for all its sources (g2b)"
    ),
    days: int = typer.Option(
        30, min=1, max=731, help="Window: this many days up to --until (budget boards: ~400)"
    ),
    until: str = typer.Option(None, help="Last day of the window (YYYY-MM-DD, default today KST)"),
    rows: int = typer.Option(None, min=1, max=999, help="Items per call (조달청; default 100)"),
    max_calls: int = typer.Option(
        None, min=1, help="Stop a source after this many calls (retries count)"
    ),
    report: Path = typer.Option(None, help="Also write the JSON report here"),
) -> None:
    """Backfill sources over a window through the real adapters, as the worker's ingest_source
    job would, and report calls, retries, errors and rows per operation. Re-running the same
    window is safe: documents are upserted by (source, external_id) and unchanged ones skipped.
    Processing is separate: `manage pipeline run`."""
    from datetime import timedelta

    from app.pipeline.backfill import ingest_window, resolve_source_keys
    from app.sources.base import FetchWindow

    configure_logging(json=False, level="WARNING", stream=sys.stderr)
    last = date.fromisoformat(until) if until else today_kst()
    window = FetchWindow(last - timedelta(days=days - 1), last)

    async def go(session: Any, runtime: Any) -> list[dict[str, Any]]:
        try:
            sources = await resolve_source_keys(session, source)
        except LookupError as exc:
            raise typer.BadParameter(str(exc), param_hint="--source") from exc
        return await ingest_window(
            session, runtime, sources, window, rows=rows, max_calls=max_calls
        )

    reports = _run(lambda: _with_session(go))
    for r in reports:
        typer.echo(
            f"{r['source']:<16} {r['status']:<9} calls={r['calls']} "
            f"fetched={r.get('fetched', 0)} created={r.get('created', 0)} "
            f"updated={r.get('updated', 0)} skipped={r.get('skipped', 0)} {r['seconds']}s"
            + (f"  {r['error']}" if r["error"] else ""),
            err=True,
        )
    out = json.dumps(reports, ensure_ascii=False, indent=2, default=str)
    if report is not None:
        report.write_text(out + "\n", encoding="utf-8")
    typer.echo(out)
    if any(r["error"] for r in reports):
        raise typer.Exit(1)


@pipeline_app.command("run")
def pipeline_run(
    limit: int = typer.Option(None, min=1, help="Process at most this many documents"),
) -> None:
    """Process every pending document (parse → extract → verify → signals) and link the new
    signals into opportunities: the worker's process_document → link_signals chain in-process."""
    configure_logging(json=False, level="WARNING", stream=sys.stderr)

    async def go(session: Any, runtime: Any) -> dict[str, Any]:
        from app.pipeline.backfill import process_pending

        return await process_pending(session, runtime, limit=limit)

    result = _run(lambda: _with_session(go))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result["stopped"]:  # the LLM account needs a person; the rest is still pending
        raise typer.Exit(1)


@llm_cache_app.command("export")
def llm_cache_export(path: Path = typer.Argument(..., help="Where to write (.jsonl.gz)")) -> None:
    """Write every cached extraction answer (key + response) so a new database need not pay
    for them again."""

    async def go(session: Any, runtime: Any) -> int:
        from app.llm.cache_io import export_cache

        return await export_cache(session, path)

    typer.echo(f"exported {_run(lambda: _with_session(go))} answers to {path}")


@llm_cache_app.command("import")
def llm_cache_import(
    path: Path = typer.Argument(..., help="A file from `llm-cache export`"),
) -> None:
    """Load cached extraction answers; keys already here are kept as they are."""

    async def go(session: Any, runtime: Any) -> int:
        from app.llm.cache_io import import_cache

        return await import_cache(session, path, model=runtime.settings.llm_extract_model)

    typer.echo(f"imported {_run(lambda: _with_session(go))} new answers from {path}")


@link_app.command("export")
def link_export(
    path: Path = typer.Argument(..., help="Where to write (.jsonl.gz)"),
    doc_type: list[str] = typer.Option(
        ["budget_book", "council_minutes"], "--doc-type", "-t", help="Repeatable"
    ),
) -> None:
    """Write the signals of these document types with the opportunity each joined, for
    `link replay` in another database."""

    async def go(session: Any, runtime: Any) -> int:
        from app.pipeline.link_replay import export_signals

        return await export_signals(session, path, doc_types=doc_type)

    typer.echo(f"exported {_run(lambda: _with_session(go))} signals to {path}")


@link_app.command("reconcile")
def link_reconcile(
    institution_code: list[str] = typer.Option(
        None, "--institution-code", help="Demand owner; repeatable. Default: all pending owners"
    ),
    force: bool = typer.Option(
        False, help="Recompute selected owners even if their generation is clean"
    ),
    today: str = typer.Option(None, help="Business date YYYY-MM-DD; default KST today"),
) -> None:
    """Complete automatic link convergence, preserving reviewed/customer identities.

    This writes memberships and history atomically per owner. It neither extracts inputs
    nor sends notifications. The worker sweeper dispatches pending recommendations.
    """
    from dataclasses import asdict

    business_date = date.fromisoformat(today) if today else today_kst()

    async def go(session: Any, runtime: Any) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from app.db.models import LinkReconciliationState
        from app.pipeline.link_reconcile import mark_link_dirty, reconcile_institution

        query = select(LinkReconciliationState.institution_code)
        if institution_code:
            query = query.where(LinkReconciliationState.institution_code.in_(institution_code))
        elif not force:
            query = query.where(
                LinkReconciliationState.generation > LinkReconciliationState.reconciled_generation
            )
        codes = list(
            await session.scalars(query.order_by(LinkReconciliationState.institution_code))
        )
        results = []
        for code in codes:
            if force:
                await mark_link_dirty(session, {code})
            result = await reconcile_institution(session, runtime, code, today=business_date)
            await session.commit()
            results.append({"institution_code": code, **asdict(result)})
        return results

    typer.echo(json.dumps(_run(lambda: _with_session(go)), ensure_ascii=False, indent=2))


@eval_app.command("export-reviews")
def eval_export_reviews(
    out: Path = typer.Option(..., help="Write a frozen human-reviewed JSONL dataset"),
    source: list[str] = typer.Option(None, "--source"),
    doc_type: list[str] = typer.Option(None, "--doc-type"),
    limit: int = typer.Option(500, min=1, help="Maximum reviewed candidates; reports truncation"),
    split_seed: str = typer.Option("review-v1"),
    resolved_before: str = typer.Option(
        None, help="Latest judgment cutoff (ISO timestamp with timezone)"
    ),
) -> None:
    """Export grounded human decisions; never declares unreviewed fields to be gold."""
    configure_logging(json=False, level="WARNING", stream=sys.stderr)
    try:
        cutoff = datetime.fromisoformat(resolved_before) if resolved_before else None
        if cutoff is not None and cutoff.utcoffset() is None:
            raise ValueError("resolved-before must include timezone")
        if not split_seed.strip():
            raise ValueError("split-seed must not be blank")
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    async def go(session: Any, runtime: Any) -> dict[str, Any]:
        from app.eval.review_dataset import export_review_dataset

        report = await export_review_dataset(
            session,
            out,
            source_keys=source or None,
            doc_types=doc_type or None,
            limit=limit,
            split_seed=split_seed,
            resolved_before=cutoff,
        )
        return report.to_json()

    typer.echo(json.dumps(_run(lambda: _with_session(go)), ensure_ascii=False, indent=2))


@sources_app.command("coverage")
def sources_coverage(
    out: Path = typer.Option(None, help="Save the collection coverage report as JSON"),
    verify_raw: bool = typer.Option(
        True, help="Check local raw files; remote storage stays unverified"
    ),
) -> None:
    """Distinguish configured institutions from collected documents and available originals."""
    configure_logging(json=False, level="WARNING", stream=sys.stderr)

    async def go(session: Any, runtime: Any) -> dict[str, Any]:
        from app.sources.coverage import coverage_report

        return await coverage_report(session, verify_raw=verify_raw)

    payload = json.dumps(_run(lambda: _with_session(go)), ensure_ascii=False, indent=2)
    if out:
        out.write_text(payload + "\n", encoding="utf-8")
    typer.echo(payload)


@eval_app.command("holdout")
def eval_holdout(
    manifest: Path = typer.Argument(..., exists=True, dir_okay=False),
    code_revision: str = typer.Option(..., help="Evaluated code revision"),
    out: Path = typer.Option(None, help="Write full predictions and metrics as JSON"),
) -> None:
    """Free, no-network extraction audit on hash-verified real-document excerpts."""
    from app.eval.holdout import evaluate_holdout

    try:
        result = _run(lambda: evaluate_holdout(manifest, code_revision=code_revision))
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if out:
        try:
            with out.open("x", encoding="utf-8") as handle:
                handle.write(payload + "\n")
        except OSError as exc:
            raise typer.BadParameter(
                "--out must be a new writable file; evidence is never overwritten"
            ) from exc
    typer.echo(payload)


@eval_app.command("longitudinal")
def eval_longitudinal(
    snapshot: Path = typer.Argument(..., exists=True, dir_okay=False),
    observations: Path = typer.Option(None, exists=True, dir_okay=False),
    horizon_days: int = typer.Option(540, min=1),
    as_of: str = typer.Option(None, help="Observation cutoff YYYY-MM-DD; cannot be future"),
    out: Path = typer.Option(None, help="Write cohort, censoring and lineage report"),
) -> None:
    """Follow up frozen forecasts; missing or immature outcomes never count as negatives."""
    from app.eval.longitudinal import evaluate_longitudinal

    try:
        result = evaluate_longitudinal(
            snapshot,
            observations,
            horizon_days=horizon_days,
            as_of=date.fromisoformat(as_of) if as_of else None,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if out:
        try:
            with out.open("x", encoding="utf-8") as handle:
                handle.write(payload + "\n")
        except OSError as exc:
            raise typer.BadParameter(
                "--out must be a new writable file; evidence is never overwritten"
            ) from exc
    typer.echo(payload)


@eval_app.command("freeze")
def eval_freeze(
    out: Path = typer.Option(..., help="Atomically write the current observation snapshot"),
    institution_code: str = typer.Option(..., help="Institution scope, e.g. LG-41130"),
    code_revision: str = typer.Option(..., help="Operator-attested deployed Git revision"),
    max_signals: int = typer.Option(
        10000, min=1, help="Abort instead of writing a partial snapshot"
    ),
) -> None:
    """Freeze current source/link/prediction inputs for later outcome evaluation; no backdating."""
    configure_logging(json=False, level="WARNING", stream=sys.stderr)
    if not institution_code.strip() or not code_revision.strip():
        raise typer.BadParameter("institution-code and code-revision must not be blank")

    async def go(session: Any, runtime: Any) -> dict[str, Any]:
        from app.eval.forecast_snapshot import export_forecast_snapshot

        report = await export_forecast_snapshot(
            session,
            out,
            institution_code=institution_code,
            code_revision=code_revision,
            max_signals=max_signals,
            settings=runtime.settings,
        )
        return report.to_json()

    typer.echo(json.dumps(_run(lambda: _with_session(go)), ensure_ascii=False, indent=2))


class LinkReplayOrder(StrEnum):
    PUBLICATION = "publication"
    REVERSE = "reverse"


@link_app.command("replay")
def link_replay(
    path: Path = typer.Argument(..., help="A file from `link export`"),
    first: list[str] = typer.Option(
        None, "--first", help="Document types linked in a run of their own before the rest"
    ),
    out: Path = typer.Option(None, help="Write the resulting opportunities (signal keys) here"),
    keep: bool = typer.Option(False, help="Commit, to look at the opportunities afterwards"),
    order: LinkReplayOrder = typer.Option(LinkReplayOrder.PUBLICATION, help="Arrival order audit"),
    today: str = typer.Option(None, help="Fixed evaluation date (YYYY-MM-DD, default KST today)"),
) -> None:
    """Link the exported signals with the current linker in a seeded database that has no
    opportunities, and compare with the run they came from. Rolled back unless --keep."""
    configure_logging(json=False, level="WARNING", stream=sys.stderr)

    from app.pipeline.link_replay import ReplayOrder, ReplayResult, load_export, replay_batches

    records = load_export(path)
    replay_order: ReplayOrder = "reverse" if order == LinkReplayOrder.REVERSE else "publication"
    try:
        replay_batches(records, first=first or None, order=replay_order)
        evaluation_date = date.fromisoformat(today) if today else today_kst()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    async def go(session: Any, runtime: Any) -> ReplayResult:
        from sqlalchemy import func, select

        from app.db.models import Opportunity
        from app.pipeline.link_replay import replay_links

        if await session.scalar(select(func.count()).select_from(Opportunity)):
            # They would be candidates too, and the result would not be the file's alone.
            typer.echo("this database already has opportunities; replay into a fresh one", err=True)
            raise typer.Exit(2)
        result = await replay_links(
            session,
            runtime,
            records,
            first=first or None,
            order=replay_order,
            today=evaluation_date,
        )
        if not keep:
            await session.rollback()
        return result

    result = _run(lambda: _with_session(go))
    if out:
        out.write_text(json.dumps(result.groups, ensure_ascii=False) + "\n", encoding="utf-8")
    typer.echo(json.dumps(result.summary(), ensure_ascii=False, indent=2))


@pipeline_app.command("reresolve")
def pipeline_reresolve() -> None:
    """Resolve institutions again for documents that had none, with the current table, and
    queue those that resolve for `manage pipeline run`. Nothing is refetched."""
    configure_logging(json=False, level="WARNING", stream=sys.stderr)

    async def go(session: Any, runtime: Any) -> dict[str, Any]:
        from app.pipeline.ingest import reresolve_institutions

        return await reresolve_institutions(session, runtime)

    typer.echo(json.dumps(_run(lambda: _with_session(go)), ensure_ascii=False, indent=2))


@pipeline_app.command("revalidate")
def pipeline_revalidate(
    apply: bool = typer.Option(False, "--apply", help="Apply exactly the reviewed dry-run"),
    expected_digest: str = typer.Option(None, help="Digest printed by the matching dry-run"),
    source: list[str] = typer.Option(None, "--source", "-s", help="Repeatable source key"),
    doc_type: list[str] = typer.Option(None, help="council_minutes or budget_book; repeatable"),
    limit: int = typer.Option(1000, min=1, max=10000),
    after_id: int = typer.Option(0, min=0),
    today: str = typer.Option(None, help="Fixed business date (YYYY-MM-DD)"),
    out: Path = typer.Option(None, help="Write the audit/apply JSON report"),
) -> None:
    """Audit stored evidence without model calls. Default is read-only; preserve human history.

    Use identical scope/date and --expected-digest for --apply. Repeat with --after-id from
    next_after_id while has_more is true, auditing each page before applying it.
    """
    from app.pipeline.revalidate import revalidate_signals

    if apply != bool(expected_digest):
        raise typer.BadParameter("--apply and --expected-digest must be supplied together")
    configure_logging(json=False, level="WARNING", stream=sys.stderr)

    async def go(session: Any, runtime: Any) -> dict[str, Any]:
        result = await revalidate_signals(
            session,
            runtime,
            apply=apply,
            expected_digest=expected_digest,
            source_keys=source,
            doc_types=doc_type,
            limit=limit,
            after_id=after_id,
            today=date.fromisoformat(today) if today else None,
        )
        if not apply:
            await session.rollback()
        return result.to_json()

    try:
        result = _run(lambda: _with_session(go))
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if out is not None:
        out.write_text(rendered + "\n", encoding="utf-8")
    typer.echo(rendered)


def _stt(spec: str) -> Any:
    from app.media.stt import stt_from_spec

    settings = get_settings()
    try:
        return stt_from_spec(
            spec,
            model=settings.stt_model,
            model_dir=settings.stt_model_dir,
            compute_type=settings.stt_compute_type,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--stt") from exc


def _media_workdir(video: Path, workdir: Path | None) -> Path:
    """One directory per video (name + size), so re-running a command finds its checkpoints."""
    return workdir or Path(get_settings().media_workdir) / f"{video.stem}-{video.stat().st_size}"


async def _transcribe(video: Path, work: Path, stt: str, ocr: Any) -> Any:
    from app.media.job import DirCheckpoints, MediaOptions, transcribe_media

    settings = get_settings()

    async def progress(stage: str, done: int, total: int) -> None:
        typer.echo(f"{stage} {done}/{total}", err=True)

    return await transcribe_media(
        video,
        workdir=work,
        stt=_stt(stt),
        ocr=ocr,
        checkpoints=DirCheckpoints(work / "checkpoints"),
        options=MediaOptions.from_settings(settings),
        progress=progress,
    )


@media_app.command("transcribe")
def media_transcribe(
    video: Path = typer.Argument(..., exists=True, dir_okay=False),
    stt: str = typer.Option("faster-whisper", help="faster-whisper[:MODEL] or fixture:PATH"),
    workdir: Path = typer.Option(
        None, help="Checkpoints and scratch (default under media_workdir)"
    ),
    out: Path = typer.Option(None, help="Also write the transcript text here"),
) -> None:
    """Transcribe one meeting video. Every window is checkpointed: run the same command again
    after a crash and it resumes where it stopped."""
    from app.runtime import build_ocr

    configure_logging(json=False, level="WARNING", stream=sys.stderr)
    work = _media_workdir(video, workdir)
    media = _run(lambda: _transcribe(video, work, stt, build_ocr(get_settings())))
    if out is not None:
        out.write_text(media.transcript.text, encoding="utf-8")
    typer.echo(json.dumps(media.summary(), ensure_ascii=False, indent=2))


@media_app.command("ingest")
def media_ingest(
    video: Path = typer.Argument(..., exists=True, dir_okay=False),
    title: str = typer.Option(..., help="제311회 본회의 제2차 …"),
    meeting_date: str = typer.Option(..., help="YYYY-MM-DD"),
    publisher: str = typer.Option("경기도 성남시의회", help="Council name as printed"),
    institution: str = typer.Option(None, help="Institution code hint (CN-41130)"),
    url: str = typer.Option(None, help="Where the video is published"),
    published_at: str = typer.Option(
        None, help="YYYY-MM-DD the video went public (else the meeting date, marked uncertain)"
    ),
    external_id: str = typer.Option(None, help="Stable id (default: video name + date)"),
    stt: str = typer.Option("faster-whisper", help="faster-whisper[:MODEL] or fixture:PATH"),
    workdir: Path = typer.Option(None),
) -> None:
    """Transcribe, store as a council minutes document, process it, and print its signals with
    the seconds of video their evidence covers."""
    from app.media.ingest import ingest_transcript, transcript_record

    configure_logging(json=False, level="WARNING", stream=sys.stderr)
    work = _media_workdir(video, workdir)

    async def go(session: Any, runtime: Any) -> dict[str, Any]:
        media = await _transcribe(video, work, stt, runtime.ocr)
        record = transcript_record(
            media,
            external_id=external_id or f"{video.stem}:{meeting_date}",
            title=title,
            meeting_date=date.fromisoformat(meeting_date),
            publisher_raw=publisher,
            institution_code_hint=institution,
            url=url,
            published_at=date.fromisoformat(published_at) if published_at else None,
        )
        result = await ingest_transcript(session, runtime, record)
        return {
            "document_id": result.document_id,
            "action": result.action,
            "media": media.summary(),
            "signals": result.signals,
        }

    typer.echo(json.dumps(_run(lambda: _with_session(go)), ensure_ascii=False, indent=2))


@media_app.command("synthetic")
def media_synthetic(out: Path = typer.Argument(..., file_okay=False)) -> None:
    """Write the synthetic 36-second meeting (meeting.mp4, meeting.m4a, segments.json) used by
    the tests and the queue E2E. Needs ffmpeg and a Korean font."""
    from app.media.synthetic import build_meeting

    meeting = build_meeting(out)
    typer.echo(
        json.dumps(
            {
                "video": str(meeting.video),
                "audio_only": str(meeting.audio_only),
                "segments": str(meeting.segments_file),
                "duration": meeting.duration,
            },
            indent=2,
        )
    )


@queue_app.command("worker")
def queue_worker(
    worker_id: str = typer.Option(None, help="Default: host name + pid"),
    once: bool = typer.Option(False, help="Run at most one job, then exit (tests, cron)"),
    lease_seconds: int = typer.Option(60, min=10),
    heartbeat_seconds: float = typer.Option(15.0, min=0.1),
) -> None:
    """Claim and run media.transcribe jobs until SIGTERM/SIGINT. A job in progress on shutdown
    goes straight back to the queue; one whose worker died is reaped when its lease expires."""
    import os
    import signal
    import socket

    from app.media.worker import KIND, transcribe_job
    from app.queue.pg import Job
    from app.queue.worker import Handler, JobContext, WorkerConfig, run_worker, work_one

    configure_logging(json=get_settings().log_json, level="INFO", stream=sys.stderr)
    config = WorkerConfig(
        worker_id=worker_id or f"{socket.gethostname()}:{os.getpid()}",
        lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds,
    )

    async def go(session: Any, runtime: Any) -> str | None:
        async def media(job: Job, ctx: JobContext) -> dict[str, Any]:
            return await transcribe_job(job, ctx, runtime=runtime)

        handlers: dict[str, Handler] = {KIND: media}
        if once:
            return await work_one(handlers, config)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run_worker(handlers, config, stop)
        return None

    outcome = _run(lambda: _with_session(go))
    if once:
        typer.echo(outcome or "idle")


@queue_app.command("enqueue-media")
def queue_enqueue_media(
    video: Path = typer.Argument(..., exists=True, dir_okay=False),
    org: int = typer.Option(..., help="Organisation that owns (and pays for) the job"),
    title: str = typer.Option(...),
    meeting_date: str = typer.Option(..., help="YYYY-MM-DD"),
    publisher: str = typer.Option("경기도 성남시의회"),
    url: str = typer.Option(None),
    published_at: str = typer.Option(None, help="YYYY-MM-DD the video went public"),
    stt: str = typer.Option("faster-whisper", help="faster-whisper[:MODEL] or fixture:PATH"),
    budget_usd: float = typer.Option(
        None, help="Fail before transcribing if the estimate is higher"
    ),
) -> None:
    """Queue one video. The same file (by content hash) for the same organisation returns the
    job that already has it."""
    from app.media.worker import KIND, sha256_file
    from app.queue.pg import enqueue

    digest = sha256_file(video)
    location = f"file://{video.resolve()}"

    async def go(session: Any, runtime: Any) -> dict[str, Any]:
        job_id, created = await enqueue(
            session,
            org_id=org,
            kind=KIND,
            payload={
                "video": location,
                "sha256": digest,
                "title": title,
                "meeting_date": meeting_date,
                "publisher": publisher,
                "url": url,
                "published_at": published_at,
                "stt": stt,
            },
            dedupe_key=f"sha256:{digest}",
            budget_usd=budget_usd,
        )
        return {"job_id": job_id, "created": created}

    typer.echo(json.dumps(_run(lambda: _with_session(go)), ensure_ascii=False))


@queue_app.command("reap")
def queue_reap() -> None:
    """Return jobs whose worker stopped heartbeating to the queue (workers also do this)."""

    async def go(session: Any, runtime: Any) -> int:
        from app.queue.pg import reap

        return await reap(session)

    typer.echo(f"reaped {_run(lambda: _with_session(go))} jobs")


@apikey_app.command("create")
def apikey_create(
    org: int = typer.Option(...),
    name: str = typer.Option(..., help="What the key is for (shown in the console)"),
    scope: list[str] = typer.Option(..., "--scope", help="jobs:read, jobs:write, usage:read"),
) -> None:
    """Create a key and print it once. Only its hash is stored."""
    from app.queue.keys import create_api_key

    async def go(session: Any, runtime: Any) -> str:
        return await create_api_key(session, org_id=org, name=name, scopes=scope)

    try:
        typer.echo(_run(lambda: _with_session(go)))
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--scope") from exc


@apikey_app.command("revoke")
def apikey_revoke(prefix: str = typer.Argument(..., help="pfk_xxxxxxxx")) -> None:
    from app.queue.keys import revoke_api_key

    async def go(session: Any, runtime: Any) -> bool:
        return await revoke_api_key(session, prefix)

    if not _run(lambda: _with_session(go)):
        raise typer.Exit(1)
    typer.echo(f"revoked {prefix}")


@app.command()
def bench(
    scale: float = typer.Option(1.0, help="Multiply the default row counts"),
    report: Path = typer.Option(Path("performance.md"), help="Markdown report to write"),
) -> None:
    """Load a throwaway <db>_bench at production-like volume and compare hot query plans
    between the initial schema and head (see docs/performance.md)."""
    from app.bench import SIZES, run_bench

    configure_logging(json=False, level="WARNING", stream=sys.stderr)
    sizes = {k: max(1, int(v * scale)) for k, v in SIZES.items()}
    results = _run(lambda: run_bench(sizes, report, log=typer.echo))
    typer.echo(json.dumps(results, ensure_ascii=False, indent=2, default=str))


@ops_app.command("bootstrap")
def ops_bootstrap() -> None:
    """Institutions and the live sources whose keys are set; refuses a demo-seeded database."""
    from app.db.session import dispose_engine
    from app.ops import bootstrap

    configure_logging(json=False, level="WARNING", stream=sys.stderr)

    async def go() -> dict[str, Any]:
        try:
            return await bootstrap(get_settings())
        finally:
            await dispose_engine()

    try:
        result = _run(go)
    except RuntimeError as exc:
        typer.echo(f"Refused: {exc}", err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(result, ensure_ascii=False))


@ops_app.command("tick")
def ops_tick(
    max_minutes: float = typer.Option(
        50.0, min=1, help="Start no new job after this long; durable state carries over"
    ),
    first_window_days: int = typer.Option(
        7, min=1, max=30, help="Days a source that was never fetched backfills"
    ),
    storage_limit_mb: float = typer.Option(
        None, min=1, help="Database size the host allows; fetching pauses at 90% of it"
    ),
    prune_raw: bool = typer.Option(
        False, help="Delete originals of processed documents (file:// store on a CI runner)"
    ),
    since: str = typer.Option(None, help="Look for due crons from here (ISO), not the last pass"),
    report: Path = typer.Option(None, help="Also write the JSON report here"),
) -> None:
    """Run the crons due since the previous pass with the worker's own schedule and jobs, then
    every job they enqueue, in this process. Exit 1 when a job failed (retries are not
    failures)."""
    from app.db.session import dispose_engine
    from app.ops import tick

    configure_logging(json=False, level="WARNING", stream=sys.stderr)

    async def go() -> dict[str, Any]:
        try:
            result = await tick(
                get_settings(),
                since=datetime.fromisoformat(since) if since else None,
                max_minutes=max_minutes,
                first_window_days=first_window_days,
                storage_limit_mb=storage_limit_mb,
                prune=prune_raw,
            )
            return result.as_dict()
        finally:
            await dispose_engine()

    result = _run(go)
    out = json.dumps(result, ensure_ascii=False, indent=2)
    if report is not None:
        report.write_text(out + "\n", encoding="utf-8")
    typer.echo(out)
    if result["failed"]:
        raise typer.Exit(1)


@app.command()
def worker() -> None:
    """Run the arq worker (queue + cron). Serves GET /healthz on $PORT when set (Cloud Run)."""
    from app.worker.runner import run

    run()


@app.command()
def openapi() -> None:
    """Print the OpenAPI schema (used to generate the web app's TypeScript types)."""
    from app.api.app import create_app

    configure_logging(json=False, level="WARNING", stream=sys.stderr)
    typer.echo(json.dumps(create_app().openapi(), ensure_ascii=False, indent=2))


if __name__ == "__main__":  # pragma: no cover
    app()
