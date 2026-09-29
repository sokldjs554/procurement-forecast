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
manage worker                     # arq worker + cron (+ /healthz on $PORT for Cloud Run)
manage openapi > openapi.json     # schema for the web app's generated types
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Awaitable, Callable, Coroutine
from datetime import date
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
app.add_typer(db_app, name="db")
app.add_typer(demo_app, name="demo")
app.add_typer(eval_app, name="eval")
app.add_typer(sources_app, name="sources")
app.add_typer(pipeline_app, name="pipeline")
app.add_typer(llm_cache_app, name="llm-cache")
app.add_typer(link_app, name="link")

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


@link_app.command("replay")
def link_replay(
    path: Path = typer.Argument(..., help="A file from `link export`"),
    first: list[str] = typer.Option(
        None, "--first", help="Document types linked in a run of their own before the rest"
    ),
    out: Path = typer.Option(None, help="Write the resulting opportunities (signal keys) here"),
    keep: bool = typer.Option(False, help="Commit, to look at the opportunities afterwards"),
) -> None:
    """Link the exported signals with the current linker in a seeded database that has no
    opportunities, and compare with the run they came from. Rolled back unless --keep."""
    configure_logging(json=False, level="WARNING", stream=sys.stderr)

    from app.pipeline.link_replay import ReplayResult, load_export

    records = load_export(path)

    async def go(session: Any, runtime: Any) -> ReplayResult:
        from sqlalchemy import func, select

        from app.db.models import Opportunity
        from app.pipeline.link_replay import replay_links

        if await session.scalar(select(func.count()).select_from(Opportunity)):
            # They would be candidates too, and the result would not be the file's alone.
            typer.echo("this database already has opportunities; replay into a fresh one", err=True)
            raise typer.Exit(2)
        result = await replay_links(
            session, runtime, records, first=first or None, today=today_kst()
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
