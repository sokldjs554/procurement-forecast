"""Operator console API (staff only): pipeline health, sources, jobs, review queue, LLM spend,
evaluation results. Everything an on-call engineer needs to answer "is the pipeline healthy,
and if not, where did it break?" without opening a database shell."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import Date, cast, func, select

from app.api.deps import QueueDep, RuntimeDep, SessionDep, StaffDep
from app.api.presenters import signal_out
from app.api.schemas import (
    DocumentDetailOut,
    EvalRunOut,
    FunnelOut,
    JobRunOut,
    LLMUsageOut,
    LLMUsageRow,
    OverviewOut,
    ReviewDecisionIn,
    ReviewItemOut,
    SourceOut,
)
from app.clock import now_utc
from app.db.models import (
    Document,
    DocumentChunk,
    EvalRun,
    IngestRun,
    InstitutionRow,
    JobRun,
    LLMCall,
    Opportunity,
    OpportunitySignal,
    ReviewItem,
    Signal,
    Source,
)
from app.sources.resilience import RedisBreaker
from app.worker.queue import QUEUE, enqueue

router = APIRouter(prefix="/api/admin", tags=["admin"])


async def _funnel(session: SessionDep) -> FunnelOut:
    docs = dict(
        (
            await session.execute(
                select(Document.doc_type, func.count()).group_by(Document.doc_type)
            )
        ).all()
    )
    # One pass over the largest table instead of two (docs/performance.md).
    chunk_counts = (
        await session.execute(
            select(func.count(), func.count().filter(DocumentChunk.triage_passed.is_(True)))
        )
    ).one()
    chunks, triaged = int(chunk_counts[0]), int(chunk_counts[1])
    signals = dict(
        (await session.execute(select(Signal.verdict, func.count()).group_by(Signal.verdict))).all()
    )
    opps = dict(
        (
            await session.execute(
                select(Opportunity.status, func.count()).group_by(Opportunity.status)
            )
        ).all()
    )
    review_open = (
        await session.scalar(
            select(func.count()).select_from(ReviewItem).where(ReviewItem.status == "open")
        )
        or 0
    )
    return FunnelOut(
        documents=docs,
        chunks=chunks,
        chunks_triaged=triaged,
        signals=signals,
        opportunities=opps,
        review_open=review_open,
    )


@router.get("/overview", response_model=OverviewOut)
async def overview(_: StaffDep, session: SessionDep, runtime: RuntimeDep) -> OverviewOut:
    since = datetime.now(UTC) - timedelta(hours=24)
    jobs = dict(
        (
            await session.execute(
                select(JobRun.status, func.count())
                .where(JobRun.started_at >= since)
                .group_by(JobRun.status)
            )
        ).all()
    )
    failed = (
        await session.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(JobRun.status == "failed", JobRun.started_at >= since)
        )
        or 0
    )
    spent = float(await runtime.llm.guard.spent_today())
    depth: int | None = None
    if runtime.redis is not None:
        depth = int(await runtime.redis.zcard(QUEUE))
    unhealthy = (
        await session.scalar(
            select(func.count())
            .select_from(Source)
            .where(Source.enabled.is_(True), Source.consecutive_failures > 0)
        )
        or 0
    )
    return OverviewOut(
        funnel=await _funnel(session),
        jobs_last_24h=jobs,
        failed_jobs=failed,
        llm_spent_today_usd=round(spent, 4),
        llm_daily_budget_usd=runtime.settings.llm_daily_budget_usd,
        queue_depth=depth,
        sources_unhealthy=unhealthy,
    )


@router.get("/sources", response_model=list[SourceOut])
async def sources(_: StaffDep, session: SessionDep, runtime: RuntimeDep) -> list[SourceOut]:
    rows = (await session.scalars(select(Source).order_by(Source.key))).all()
    counts = dict(
        (
            await session.execute(
                select(Document.source_id, func.count()).group_by(Document.source_id)
            )
        ).all()
    )
    out = []
    for src in rows:
        last = await session.scalar(
            select(IngestRun)
            .where(IngestRun.source_id == src.id)
            .order_by(IngestRun.started_at.desc())
            .limit(1)
        )
        circuit: dict[str, Any] = {"state": "unknown"}
        if isinstance(runtime.breaker, RedisBreaker):
            circuit = await runtime.breaker.state(src.key)
        out.append(
            SourceOut(
                key=src.key,
                name=src.name,
                adapter=src.adapter,
                enabled=src.enabled,
                last_run_at=src.last_run_at,
                last_success_at=src.last_success_at,
                consecutive_failures=src.consecutive_failures,
                circuit=circuit,
                documents=int(counts.get(src.id, 0)),
                last_run={
                    "status": last.status,
                    "fetched": last.fetched,
                    "created": last.created,
                    "updated": last.updated,
                    "error": last.error,
                    "started_at": last.started_at.isoformat(),
                }
                if last
                else None,
            )
        )
    return out


class SourceRunIn(BaseModel):
    since: date | None = None
    until: date | None = None


@router.post("/sources/{key}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_source(
    key: str, body: SourceRunIn, _: StaffDep, session: SessionDep, queue: QueueDep
) -> dict[str, Any]:
    src = await session.scalar(select(Source).where(Source.key == key))
    if src is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found")
    job_id = await enqueue(
        queue,
        "ingest_source",
        key,
        body.since.isoformat() if body.since else None,
        body.until.isoformat() if body.until else None,
        job_id=f"ingest:{key}:manual:{datetime.now(UTC):%Y%m%d%H%M}",
    )
    return {"job_id": job_id}


class SourcePatchIn(BaseModel):
    enabled: bool


@router.patch("/sources/{key}", response_model=dict)
async def patch_source(
    key: str, body: SourcePatchIn, _: StaffDep, session: SessionDep
) -> dict[str, Any]:
    src = await session.scalar(select(Source).where(Source.key == key))
    if src is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found")
    src.enabled = body.enabled
    return {"key": key, "enabled": src.enabled}


@router.get("/jobs", response_model=list[JobRunOut])
async def jobs(
    _: StaffDep,
    session: SessionDep,
    status_: Annotated[str | None, Query(alias="status")] = None,
    job: str | None = None,
    limit: Annotated[int, Query(le=200)] = 100,
) -> list[JobRunOut]:
    q = select(JobRun).order_by(JobRun.started_at.desc()).limit(limit)
    if status_:
        q = q.where(JobRun.status == status_)
    if job:
        q = q.where(JobRun.job == job)
    return [JobRunOut.model_validate(r) for r in await session.scalars(q)]


@router.post("/jobs/{run_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_job(
    run_id: int, _: StaffDep, session: SessionDep, queue: QueueDep
) -> dict[str, Any]:
    run = await session.get(JobRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job run not found")
    args = run.args.get("raw", [])
    job_id = await enqueue(
        queue, run.job, *args, job_id=f"retry:{run.id}:{datetime.now(UTC):%H%M%S}"
    )
    return {"job_id": job_id}


async def _review_out(session: SessionDep, item: ReviewItem) -> ReviewItemOut:
    signal = await session.get(Signal, item.signal_id)
    assert signal is not None
    doc = await session.get(Document, signal.document_id)
    assert doc is not None
    chunk = await session.get(DocumentChunk, signal.chunk_id) if signal.chunk_id else None
    inst = (
        await session.get(InstitutionRow, signal.institution_code)
        if signal.institution_code
        else None
    )
    return ReviewItemOut(
        id=item.id,
        status=item.status,
        reasons=item.reasons,
        created_at=item.created_at,
        signal=signal_out(signal, doc, chunk, None),
        institution_name=inst.name if inst else None,
    )


@router.get("/review", response_model=list[ReviewItemOut])
async def review_queue(
    _: StaffDep,
    session: SessionDep,
    status_: Annotated[str, Query(alias="status")] = "open",
    limit: Annotated[int, Query(le=100)] = 50,
) -> list[ReviewItemOut]:
    items = await session.scalars(
        select(ReviewItem)
        .where(ReviewItem.status == status_)
        .order_by(ReviewItem.created_at)
        .limit(limit)
    )
    return [await _review_out(session, i) for i in items]


@router.post("/review/{item_id}", response_model=ReviewItemOut)
async def decide_review(
    item_id: int,
    body: ReviewDecisionIn,
    principal: StaffDep,
    session: SessionDep,
    runtime: RuntimeDep,
) -> ReviewItemOut:
    from app.pipeline.review import reconcile_reviewed_signal

    item = await session.get(ReviewItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "review item not found")
    # Same lock order as document reprocessing: evidence first, then its review.
    signal = await session.scalar(
        select(Signal)
        .where(Signal.id == item.signal_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if signal is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "signal changed; reload review")
    await session.refresh(item, with_for_update=True)
    old_link = await session.scalar(
        select(OpportunitySignal).where(OpportunitySignal.signal_id == signal.id)
    )
    previous_link = (
        {
            "opportunity_id": old_link.opportunity_id,
            "method": old_link.method,
            "score": old_link.score,
            "tentative": old_link.tentative,
            "reasons": old_link.reasons,
        }
        if old_link is not None
        else None
    )
    previous_resolution = {k: v for k, v in item.resolution.items() if k != "history"}
    history = list(item.resolution.get("history", []))
    if previous_resolution:
        history.append(
            previous_resolution
            | {
                "resolved_by": item.resolved_by,
                "resolved_at": item.resolved_at.isoformat() if item.resolved_at else None,
            }
        )
    changes: dict[str, Any] = {}
    if body.action == "reject":
        signal.verdict = "rejected"
        item.status = "rejected"
    else:
        for field in (
            "title",
            "budget_krw",
            "expected_year",
            "commitment",
            "institution_code",
            "category",
        ):
            value = getattr(body, field)
            if value is not None:
                changes[field] = {"from": getattr(signal, field), "to": value}
                setattr(signal, field, value)
                if field == "title" and changes[field]["from"] != value:
                    # An old vector must not assert semantic similarity for a corrected title.
                    signal.embedding = None
        signal.verdict = "accepted"
        item.status = "edited" if changes else "approved"
    item.resolution = {
        "action": body.action,
        "changes": changes,
        "previous_link": previous_link,
        "history": history,
    }
    item.resolved_by = principal.user.id
    item.resolved_at = now_utc()
    await session.flush()
    await reconcile_reviewed_signal(session, runtime, signal)
    await session.commit()
    return await _review_out(session, item)


@router.get("/llm/usage", response_model=LLMUsageOut)
async def llm_usage(
    _: StaffDep,
    session: SessionDep,
    runtime: RuntimeDep,
    days: Annotated[int, Query(ge=1, le=90)] = 14,
) -> LLMUsageOut:
    since = datetime.now(UTC) - timedelta(days=days)
    day = cast(LLMCall.created_at, Date)
    rows = (
        await session.execute(
            select(
                day,
                LLMCall.task,
                LLMCall.model,
                LLMCall.status,
                func.count(),
                func.sum(LLMCall.input_tokens),
                func.sum(LLMCall.output_tokens),
                func.sum(LLMCall.cache_read_tokens),
                func.sum(LLMCall.cost_usd),
            )
            .where(LLMCall.created_at >= since)
            .group_by(day, LLMCall.task, LLMCall.model, LLMCall.status)
            .order_by(day)
        )
    ).all()
    usage = [
        LLMUsageRow(
            day=d,
            task=t,
            model=m,
            status=s,
            calls=c,
            input_tokens=int(i or 0),
            output_tokens=int(o or 0),
            cache_read_tokens=int(cr or 0),
            cost_usd=float(cost or 0),
        )
        for d, t, m, s, c, i, o, cr, cost in rows
    ]
    total = sum(r.calls for r in usage if r.task == "extract")
    hits = sum(r.calls for r in usage if r.task == "extract" and r.status == "cache_hit")
    degraded = sum(
        r.calls
        for r in usage
        if r.task == "extract" and r.status in ("error", "refusal", "budget_skip", "invalid_output")
    )
    return LLMUsageOut(
        rows=usage,
        spent_today_usd=float(await runtime.llm.guard.spent_today()),
        daily_budget_usd=runtime.settings.llm_daily_budget_usd,
        cache_hit_rate=round(hits / total, 3) if total else None,
        degraded_rate=round(degraded / total, 3) if total else None,
        extractor_mode=runtime.extractor_mode,
    )


@router.get("/evals", response_model=list[EvalRunOut])
async def evals(_: StaffDep, session: SessionDep, kind: str | None = None) -> list[EvalRunOut]:
    q = select(EvalRun).order_by(EvalRun.created_at.desc()).limit(50)
    if kind:
        q = q.where(EvalRun.kind == kind)
    return [EvalRunOut.model_validate(r) for r in await session.scalars(q)]


@router.get("/documents/{document_id}", response_model=DocumentDetailOut)
async def document(document_id: int, _: StaffDep, session: SessionDep) -> DocumentDetailOut:
    doc = await session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    chunks = await session.scalars(
        select(DocumentChunk).where(DocumentChunk.document_id == doc.id).order_by(DocumentChunk.seq)
    )
    return DocumentDetailOut(
        id=doc.id,
        title=doc.title,
        doc_type=doc.doc_type,
        parse_method=doc.parse_method,
        text_quality=doc.text_quality,
        text=doc.text,
        chunks=[
            {
                "seq": c.seq,
                "start": c.char_start,
                "end": c.char_end,
                "labels": c.labels,
                "triage_score": c.triage_score,
                "triage_passed": c.triage_passed,
            }
            for c in chunks
        ],
        structured={k: v for k, v in doc.structured.items() if k != "raw"},
    )
