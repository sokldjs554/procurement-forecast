"""Preparation and live checks, executed inside the isolated review API container only."""

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
from app.clock import today_kst
from app.db.models import (
    Brief,
    Document,
    JobRun,
    Opportunity,
    OpportunitySignal,
    Recommendation,
    Signal,
    Source,
)
from app.db.session import dispose_engine, session_scope
from app.settings import get_settings
from app.sources.registry import FIXTURE_CATALOG
from app.storage import load_raw
from app.worker.queue import create_queue
from sqlalchemy import func, select

MARKER = Path("/app/.data/review-ready.json")


def guard():
    s = get_settings()
    if not (
        s.env == "local"
        and s.database_url == "postgresql+asyncpg://review:review-local@db:5432/review"
        and s.redis_url == "redis://redis:6379/0"
        and s.storage_url == "file:///app/.data/raw"
        and s.llm_provider == "heuristic"
        and s.embedding_provider == "hashing"
        and s.payment_provider == "fake"
        and s.smtp_host == "mailpit"
        and s.smtp_port == 1025
        and s.llm_daily_budget_usd == 0
    ):
        raise RuntimeError(
            "This command only accepts the isolated, offline review configuration"
        )
    if any(
        (
            s.anthropic_api_key,
            s.voyage_api_key,
            s.toss_secret_key,
            s.solapi_api_key,
            s.data_go_kr_service_key,
            s.clik_api_key,
            s.lofin_api_key,
            s.sentry_dsn,
        )
    ):
        raise RuntimeError(
            "External credentials are forbidden in the free review stack"
        )


async def document_fingerprint(doc):
    raw_digest = None
    if doc.raw_uri:
        raw_digest = hashlib.sha256(await load_raw(doc.raw_uri)).hexdigest()
    elif (
        doc.mime.split(";", 1)[0].strip().lower() != "application/json"
        or not doc.structured
    ):
        raise RuntimeError(f"Document {doc.id} has no raw evidence")
    # Procurement JSON lives in PostgreSQL; PDF/HWP/text bytes live in the shared volume.
    # Include both forms instead of skipping structured originals or relying on row counts.
    return json.dumps(
        {
            "id": doc.id,
            "title": doc.title,
            "mime": doc.mime,
            "content_hash": doc.content_hash,
            "structured": doc.structured,
            "raw_sha256": raw_digest,
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode()


async def database_snapshot():
    async with session_scope() as session:
        external = await session.scalar(
            select(func.count())
            .select_from(Source)
            .where(
                Source.enabled.is_(True),
                Source.key.not_in([entry["key"] for entry in FIXTURE_CATALOG]),
            )
        )
        if external:
            raise RuntimeError(
                "Only fixture sources may be enabled in the offline review stack"
            )
        counts = {}
        for model in (Document, Signal, Opportunity):
            counts[model.__tablename__] = await session.scalar(
                select(func.count()).select_from(model)
            )
        if not all(counts.values()):
            raise RuntimeError(f"The review pipeline has incomplete data: {counts}")
        digest = hashlib.sha256()
        for doc in (
            await session.scalars(select(Document).order_by(Document.id))
        ).all():
            digest.update(await document_fingerprint(doc))
        # Include actual user-visible saved output, not only row counts.
        for model, columns in (
            (
                OpportunitySignal,
                (OpportunitySignal.signal_id, OpportunitySignal.opportunity_id),
            ),
            (
                Recommendation,
                (
                    Recommendation.org_id,
                    Recommendation.opportunity_id,
                    Recommendation.feedback,
                ),
            ),
            (Brief, (Brief.id, Brief.org_id, Brief.opportunity_id, Brief.content_md)),
        ):
            for row in (
                await session.execute(select(*columns).order_by(*columns[:2]))
            ).all():
                digest.update(
                    json.dumps(list(row), sort_keys=True, default=str).encode()
                )
        return {"counts": counts, "saved_data_sha256": digest.hexdigest()}


async def prepare():
    if MARKER.exists():
        print("기존 자료를 유지합니다: " + MARKER.read_text())
        await database_snapshot()
        return
    async with session_scope() as session:
        source = await session.scalar(
            select(Source).where(Source.key == "fixture_minutes")
        )
        anchor = source.config["anchor"] if source else today_kst().isoformat()
    subprocess.run(["manage", "seed", "--anchor", anchor, "--scale", "1.5"], check=True)
    # Complete pending documents left by an interrupted first preparation.
    subprocess.run(["manage", "pipeline", "run"], check=True)
    # Recommendations and the local digest must include those recovered documents too.
    subprocess.run(["manage", "demo", "run", "--anchor", anchor], check=True)
    result = await database_snapshot()
    MARKER.write_text(
        json.dumps({"anchor": anchor, "synthetic": True}, ensure_ascii=False)
    )
    print(json.dumps(result, ensure_ascii=False))


async def check():
    result = await database_snapshot()
    async with httpx.AsyncClient(base_url="http://web:3000", timeout=30) as client:
        (await client.get("/login")).raise_for_status()
        unauthorized = await client.get("/api/opportunities")
        if unauthorized.status_code != 401:
            raise RuntimeError("Unauthenticated access was not rejected")
        response = await client.post(
            "/api/auth/login",
            json={"email": "demo@example.com", "password": "demo-pass-1234"},
        )
        response.raise_for_status()
        response = await client.get("/api/opportunities")
        response.raise_for_status()
        feed = response.json()
        if not feed["items"]:
            raise RuntimeError("The real API returned an empty opportunity feed")
        opp_id = feed["items"][0]["id"]
        (await client.get(f"/api/opportunities/{opp_id}")).raise_for_status()
        if (await client.get("/api/admin/overview")).status_code != 403:
            raise RuntimeError("A regular tenant could access the operator console")
        result["feed_count"] = feed["total"]
        (await client.get("http://api:8000/readyz")).raise_for_status()
        (await client.get("http://worker:8001/healthz")).raise_for_status()
        mail = await client.get("http://mailpit:8025/api/v1/messages")
        mail.raise_for_status()
        if mail.json()["total"] < 1:
            raise RuntimeError("No synthetic pipeline mail reached the local inbox")
        result["local_mail_count"] = mail.json()["total"]
    # Prove a separate worker consumes Redis work and commits its result to PostgreSQL.
    queue = await create_queue(get_settings())
    try:
        job = await queue.enqueue_job(
            "nightly_backtest", _job_id=f"free-review:{uuid4().hex}"
        )
        if job is None:
            raise RuntimeError("Could not enqueue the review worker check")
        await job.result(timeout=90)
        async with session_scope() as session:
            row = await session.scalar(
                select(JobRun).where(JobRun.job_id == job.job_id)
            )
            if row is None or row.status != "succeeded":
                raise RuntimeError("Worker result was not committed to PostgreSQL")
        result["worker_job"] = "succeeded"
    finally:
        await queue.aclose()
    print(json.dumps(result, ensure_ascii=False, indent=2))


async def main():
    guard()
    try:
        if sys.argv[1] == "prepare":
            await prepare()
        elif sys.argv[1] == "check":
            await check()
        elif sys.argv[1] == "fingerprint":
            print(json.dumps(await database_snapshot(), sort_keys=True))
        else:
            raise ValueError("Unknown review command")
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
