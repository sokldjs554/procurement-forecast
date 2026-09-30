"""A transcribed meeting → a ``documents`` row, processed and linked like text minutes.

The transcript is stored as the document's raw content (``text/plain``) and the video facts —
timeline, captions, windows, cost — in ``structured['media']``. Re-running a job with a better
STT model produces a different transcript, a different content hash, and so a re-processed
document: the same path a corrected text minutes file takes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, OpportunitySignal, Signal, Source
from app.media.job import MediaTranscript
from app.pipeline.ingest import upsert_record
from app.pipeline.link import link_signals
from app.pipeline.link_reconcile import reconcile_pending
from app.pipeline.process import process_document
from app.runtime import Runtime
from app.sources.base import RawRecord

SOURCE: dict[str, Any] = {
    "key": "council_video",
    "name": "지방의회 인터넷방송 — 영상 회의록 (STT)",
    "adapter": "media",
}


async def media_source(session: AsyncSession) -> Source:
    source = await session.scalar(select(Source).where(Source.key == SOURCE["key"]))
    if source is None:
        source = Source(**SOURCE, config={})
        session.add(source)
        await session.flush()
    return source


def transcript_record(
    media: MediaTranscript,
    *,
    external_id: str,
    title: str,
    meeting_date: date,
    publisher_raw: str | None,
    institution_code_hint: str | None,
    url: str | None,
    published_at: date | None = None,
) -> RawRecord:
    """``published_at`` is when the video went public. Without it the meeting date stands in,
    marked so the backtest does not count it as a proven publication date (#49)."""
    return RawRecord(
        external_id=external_id,
        doc_type="council_minutes",
        title=title,
        published_at=published_at or meeting_date,
        mime="text/plain; charset=utf-8",
        publisher_raw=publisher_raw,
        institution_code_hint=institution_code_hint,
        url=url,
        content=media.transcript.text.encode("utf-8"),
        structured={
            "meeting_date": meeting_date.isoformat(),
            "published_from": "broadcast" if published_at else "meeting_date",
            "media": media.media_json(),
        },
    )


@dataclass(slots=True)
class MediaIngest:
    document_id: int
    action: str  # created | updated | skipped
    signals: list[dict[str, Any]]


async def ingest_transcript(
    session: AsyncSession, runtime: Runtime, record: RawRecord, *, process: bool = True
) -> MediaIngest:
    """Store, then process, provisionally link and complete canonical reconciliation so an
    accepted signal from a video joins an opportunity exactly as one from text minutes does.
    ``process=False`` only stores (the queue runs the rest as separate jobs)."""
    source = await media_source(session)
    doc, action = await upsert_record(session, source, record, runtime)
    signals: list[dict[str, Any]] = []
    if process and action != "skipped":
        result = await process_document(session, runtime, doc.id)
        await link_signals(session, runtime, result.signal_ids)
    if process:
        # This CLI path has no subsequent queue job to wait for. Finish before returning
        # IDs, including a retry whose document was already stored on the previous attempt.
        await reconcile_pending(session, runtime)
        signals = await signals_with_times(session, doc)
    return MediaIngest(doc.id, action, signals)


async def signals_with_times(session: AsyncSession, doc: Document) -> list[dict[str, Any]]:
    """Each signal of a video document with the seconds its evidence covers."""
    timeline = (doc.structured.get("media") or {}).get("timeline") or []
    rows = (
        await session.execute(
            select(Signal, OpportunitySignal.opportunity_id)
            .outerjoin(OpportunitySignal, OpportunitySignal.signal_id == Signal.id)
            .where(Signal.document_id == doc.id)
            .order_by(Signal.id)
        )
    ).all()
    out: list[dict[str, Any]] = []
    for sig, opportunity_id in rows:
        spans = [(e["start"], e["end"]) for e in sig.evidence if e.get("start") is not None]
        hits = [t for t in timeline for s, e in spans if t["start"] < e and s < t["end"]]
        out.append(
            {
                "signal_id": sig.id,
                "opportunity_id": opportunity_id,
                "title": sig.title,
                "verdict": sig.verdict,
                "commitment": sig.commitment,
                "budget_krw": sig.budget_krw,
                "t0": min((h["t0"] for h in hits), default=None),
                "t1": max((h["t1"] for h in hits), default=None),
                "issues": sig.grounding.get("issues", []),
            }
        )
    return out
