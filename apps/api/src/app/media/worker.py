"""``media.transcribe`` on the job queue: budget check → transcribe (checkpointed in Postgres)
→ document → signals, with the job's usage written as it completes.

The payload is written by the orchestrator, not by a tenant: it names the video file the fetch
stage stored and the STT engine the deployment allows.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.db.session import session_scope
from app.media.ffmpeg import probe
from app.media.ingest import ingest_transcript, transcript_record
from app.media.job import MediaOptions, transcribe_media
from app.media.stt import STTProvider, stt_from_spec
from app.queue.pg import Job, PgCheckpoints, month_spend_usd
from app.queue.worker import JobContext, PermanentError
from app.runtime import Runtime

KIND = "media.transcribe"


class BudgetExceededError(PermanentError):
    pass


def video_path(uri: str) -> Path:
    if uri.startswith("file://"):
        return Path(uri.removeprefix("file://"))
    if "://" in uri:
        raise PermanentError(f"unsupported video location {uri!r} (file:// only)")
    return Path(uri)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def estimate_usd(duration: float, stt: STTProvider, options: MediaOptions) -> float:
    """An upper bound before any audio is sent: every second, plus the hard-cut overlap each
    window may add (2 s per window)."""
    windows = max(1.0, duration / options.window_seconds)
    return (duration + 2.0 * windows) / 60 * stt.usd_per_audio_minute


async def check_budget(job: Job, estimate: float) -> None:
    if job.budget_usd is not None and Decimal(str(estimate)) > job.budget_usd:
        raise BudgetExceededError(
            f"estimated ${estimate:.4f} exceeds the job budget ${job.budget_usd}"
        )
    async with session_scope() as s:
        spent, cap = await month_spend_usd(s, job.org_id)
    if cap is not None and spent + Decimal(str(estimate)) > cap:
        raise BudgetExceededError(
            f"estimated ${estimate:.4f} on top of ${spent} this month exceeds the cap ${cap}"
        )


async def transcribe_job(job: Job, ctx: JobContext, *, runtime: Runtime) -> dict[str, Any]:
    settings = runtime.settings
    p = job.payload
    src = video_path(str(p["video"]))
    if not src.exists():
        raise PermanentError(f"video not found: {src}")
    try:
        stt = stt_from_spec(
            str(p.get("stt") or "faster-whisper"),
            model=settings.stt_model,
            model_dir=settings.stt_model_dir,
            compute_type=settings.stt_compute_type,
        )
    except ValueError as exc:
        raise PermanentError(str(exc)) from exc
    options = MediaOptions.from_settings(settings)
    info = await probe(src)
    if not info.has_audio:
        raise PermanentError(f"{src.name} has no audio track")
    await check_budget(job, estimate_usd(info.duration, stt, options))

    digest = str(p.get("sha256") or await asyncio.to_thread(sha256_file, src))
    media = await transcribe_media(
        src,
        workdir=Path(settings.media_workdir) / f"org{job.org_id}" / digest[:16],
        stt=stt,
        ocr=runtime.ocr,
        checkpoints=PgCheckpoints(job.org_id, f"video:{digest}"),
        options=options,
        progress=ctx.report,
    )
    cost = media.cost
    ctx.cost = cost.to_json()
    ctx.usage = [
        ("stt_audio_seconds", cost.audio_seconds_sent, cost.stt_usd),
        ("ocr_frames", float(cost.ocr_frames), 0.0),
        ("compute_seconds", cost.stt_seconds + cost.ocr_seconds + cost.ffmpeg_seconds, 0.0),
    ]
    record = transcript_record(
        media,
        external_id=str(p.get("external_id") or f"video:{digest[:16]}"),
        title=str(p["title"]),
        meeting_date=date.fromisoformat(str(p["meeting_date"])),
        publisher_raw=p.get("publisher"),
        institution_code_hint=p.get("institution"),
        url=p.get("url"),
        published_at=date.fromisoformat(str(p["published_at"])) if p.get("published_at") else None,
    )
    async with session_scope() as s:
        result = await ingest_transcript(s, runtime, record)
    return {
        "document_id": result.document_id,
        "action": result.action,
        "signals": result.signals,
        "media": media.summary(),
    }
