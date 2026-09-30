"""A broadcast meeting through the whole pipeline: video → transcript → ``documents`` row →
extraction → verification → signals that point back to seconds of the video."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy import select

from app.db.models import Document, Source
from app.db.session import session_scope
from app.media.ingest import SOURCE, ingest_transcript, transcript_record
from app.media.job import MediaOptions, MemoryCheckpoints, transcribe_media
from app.media.stt import FixtureSTT, Segment
from app.parsing.ocr import TesseractOCR


async def _ingest(runtime, meeting_video, src: Path, workdir: Path, external_id: str):  # type: ignore[no-untyped-def]
    media = await transcribe_media(
        src,
        workdir=workdir,
        stt=FixtureSTT([Segment.from_json(s) for s in meeting_video.segments]),
        ocr=TesseractOCR(),
        checkpoints=MemoryCheckpoints(),
        options=MediaOptions(window_seconds=10.0, window_search_seconds=3.0),
    )
    record = transcript_record(
        media,
        external_id=external_id,
        title="제300회 도시건설위원회 제2차 회의",
        meeting_date=date(2026, 3, 18),
        publisher_raw="경기도 성남시의회",
        institution_code_hint=None,
        url=None,
    )
    async with session_scope() as s:
        return await ingest_transcript(s, runtime, record)


def _flood_signal(signals):  # type: ignore[no-untyped-def]
    hits = [s for s in signals if "지하차도" in s["title"] or "차단" in s["title"]]
    assert len(hits) == 1, signals
    return hits[0]


async def test_meeting_video_yields_a_signal_with_its_moment(  # type: ignore[no-untyped-def]
    demo_world, runtime, meeting_video, tmp_path: Path
) -> None:
    result = await _ingest(runtime, meeting_video, meeting_video.video, tmp_path, "vid-0318")
    assert result.action == "created"
    signal = _flood_signal(result.signals)
    assert signal["verdict"] == "accepted", signal["issues"]
    assert signal["budget_krw"] == 240_000_000
    assert signal["opportunity_id"] is not None  # linked, like any accepted signal
    # The evidence is the official's answer; its seconds are where the answer is spoken.
    assert 12.0 <= signal["t0"] < signal["t1"] <= 22.0

    async with session_scope() as s:
        doc = await s.get(Document, result.document_id)
        assert doc is not None and doc.doc_type == "council_minutes"
        assert doc.structured["published_from"] == "meeting_date"
        assert doc.structured["media"]["captions"][1]["name"] == "송태아"
        assert doc.text is not None and doc.text.startswith("○조우현 의원 ")
        source = await s.get(Source, doc.source_id)
        assert source is not None and source.key == SOURCE["key"]

    # Same video, same transcript: nothing to redo.
    again = await _ingest(runtime, meeting_video, meeting_video.video, tmp_path, "vid-0318")
    assert again.action == "skipped" and again.document_id == result.document_id
    assert _flood_signal(again.signals)["signal_id"] == signal["signal_id"]


async def test_audio_only_meeting_sends_the_same_answer_to_review(  # type: ignore[no-untyped-def]
    demo_world, runtime, meeting_video, tmp_path: Path
) -> None:
    result = await _ingest(runtime, meeting_video, meeting_video.audio_only, tmp_path, "aud-0318")
    signal = _flood_signal(result.signals)
    # Nobody can say the 도로과장 said it, so it is not an executive commitment yet.
    assert signal["verdict"] == "needs_review"
    assert "official_evidence_missing" in signal["issues"]
    assert signal["opportunity_id"] is None  # review first, then linking

    async with session_scope() as s:
        docs = (
            await s.scalars(select(Document).where(Document.external_id.in_(["aud-0318"])))
        ).all()
        assert len(docs) == 1
        assert docs[0].structured["media"]["notes"] == [
            "no video track: every speaker is unknown and goes to review"
        ]
