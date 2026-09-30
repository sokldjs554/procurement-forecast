"""The media job on a real (synthetic) video: ffmpeg cuts, tesseract reads the band, a fixture
stands in for STT. What is checked is what a worker depends on: where windows fall, who is
speaking when, what a re-run after a crash pays for, and what it costs."""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest
from PIL import Image

from app.media.ffmpeg import MediaError, Window, probe
from app.media.job import DirCheckpoints, MediaOptions, transcribe_media
from app.media.stt import FixtureSTT, Segment
from app.parsing.ocr import OCRResult, TesseractOCR

# The fixture is 36 s; 10 s windows force several cuts, each in a pause between sentences.
OPTIONS = MediaOptions(window_seconds=10.0, window_search_seconds=3.0)


def _stt(meeting_video) -> FixtureSTT:  # type: ignore[no-untyped-def]
    return FixtureSTT([Segment.from_json(s) for s in meeting_video.segments])


class _WorkerKilledError(RuntimeError):
    pass


class _CountingSTT(FixtureSTT):
    """Counts calls; raises on call number ``crash_on`` (a worker killed mid-window)."""

    def __init__(self, segments: list[Segment], *, crash_on: int | None = None) -> None:
        super().__init__(segments)
        self.calls = 0
        self._crash_on = crash_on

    async def transcribe(self, audio: Path, window: Window, *, language: str) -> list[Segment]:
        self.calls += 1
        assert audio.stat().st_size > 0, "the window's audio slice was cut"
        if self.calls == self._crash_on:
            raise _WorkerKilledError
        return await super().transcribe(audio, window, language=language)


async def test_probe_reads_streams_and_rejects_non_media(meeting_video, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    info = await probe(meeting_video.video)
    assert info.has_audio and info.has_video and (info.width, info.height) == (640, 360)
    assert info.duration == pytest.approx(meeting_video.duration, abs=0.1)
    audio = await probe(meeting_video.audio_only)
    assert audio.has_audio and not audio.has_video
    junk = tmp_path / "minutes.txt"
    junk.write_text("○위원장 박재신 개의하겠습니다.", encoding="utf-8")
    with pytest.raises(MediaError, match="ffprobe exited"):
        await probe(junk)


async def test_video_becomes_minutes_with_speakers_from_the_band(  # type: ignore[no-untyped-def]
    meeting_video, tmp_path: Path
) -> None:
    media = await transcribe_media(
        meeting_video.video,
        workdir=tmp_path,
        stt=_stt(meeting_video),
        ocr=TesseractOCR(),
        checkpoints=DirCheckpoints(tmp_path / "checkpoints"),
        options=OPTIONS,
    )
    # Every cut lands in a pause, so windows abut: nothing is sent to STT twice.
    assert len(media.windows) >= 3
    assert all(w.cut in ("silence", "end") for w in media.windows)
    assert all(a.end == b.start for a, b in pairwise(media.windows))
    speech = [(s["start"], s["end"]) for s in meeting_video.segments]
    for w in media.windows[:-1]:
        assert not any(a < w.end < b for a, b in speech), f"cut at {w.end} splits a sentence"
    assert media.cost.audio_seconds_sent == pytest.approx(meeting_video.duration, abs=0.1)

    assert [(c.speaker.role, c.speaker.name) for c in media.captions] == [
        ("의원", "조우현"),
        ("도로과장", "송태아"),
        ("위원장", "박재신"),
    ]
    # The band swaps on a static shot; the tick catches it within two seconds.
    assert [c.start for c in media.captions] == pytest.approx([0.0, 12.0, 24.0], abs=2.0)
    # OCR ran once per caption, not once per sampled frame.
    assert media.caption_stats.ocr == 3 < media.caption_stats.frames

    lines = media.transcript.text.splitlines()
    assert [line.split(" ", 2)[:2] for line in lines] == [
        ["○조우현", "의원"],
        ["○도로과장", "송태아"],
        ["○위원장", "박재신"],
    ]
    assert "2억 4천만원 편성했습니다." in lines[1]
    assert not any(p.suffix == ".png" for p in tmp_path.rglob("*")), "frames are cleaned up"
    doc = media.media_json()
    assert doc["stt"] == {"provider": "fixture", "model": "recorded"}
    assert len(doc["timeline"]) == len(meeting_video.segments)


async def test_a_killed_job_resumes_without_paying_twice(meeting_video, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    segments = [Segment.from_json(s) for s in meeting_video.segments]
    store = DirCheckpoints(tmp_path / "checkpoints")

    first = _CountingSTT(segments, crash_on=3)
    with pytest.raises(_WorkerKilledError):
        await transcribe_media(
            meeting_video.video,
            workdir=tmp_path,
            stt=first,
            ocr=None,
            checkpoints=store,
            options=OPTIONS,
        )

    second = _CountingSTT(segments)
    media = await transcribe_media(
        meeting_video.video,
        workdir=tmp_path,
        stt=second,
        ocr=None,
        checkpoints=store,
        options=OPTIONS,
    )
    assert media.resumed_windows == 2  # the two windows finished before the crash
    assert second.calls == len(media.windows) - 2
    assert [s.text for s in media.segments] == [s.text for s in segments]

    third = _CountingSTT(segments)
    again = await transcribe_media(
        meeting_video.video,
        workdir=tmp_path,
        stt=third,
        ocr=None,
        checkpoints=store,
        options=OPTIONS,
    )
    assert third.calls == 0 and again.resumed_windows == len(again.windows)
    # Same recording, same models → the same document: no timing leaks into its content hash.
    assert again.media_json() == media.media_json()


async def test_changing_the_window_plan_does_not_reuse_other_cuts(  # type: ignore[no-untyped-def]
    meeting_video, tmp_path: Path
) -> None:
    segments = [Segment.from_json(s) for s in meeting_video.segments]
    store = DirCheckpoints(tmp_path / "checkpoints")
    await transcribe_media(
        meeting_video.video,
        workdir=tmp_path,
        stt=_CountingSTT(segments),
        ocr=None,
        checkpoints=store,
        options=OPTIONS,
    )
    whole = _CountingSTT(segments)
    media = await transcribe_media(
        meeting_video.video,
        workdir=tmp_path,
        stt=whole,
        ocr=None,
        checkpoints=store,
        options=MediaOptions(window_seconds=600.0),
    )
    assert len(media.windows) == 1 and whole.calls == 1 and media.resumed_windows == 0


async def test_without_a_picture_every_speaker_is_unknown(meeting_video, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    media = await transcribe_media(
        meeting_video.audio_only,
        workdir=tmp_path,
        stt=_stt(meeting_video),
        ocr=TesseractOCR(),
        checkpoints=DirCheckpoints(tmp_path / "checkpoints"),
        options=OPTIONS,
    )
    assert media.captions == []
    assert media.transcript.text.startswith("○발언자 미상 ")
    assert len(media.transcript.turns) == 1
    assert media.notes == ["no video track: every speaker is unknown and goes to review"]


class _StubOCR:
    name = "stub"

    def __init__(self) -> None:
        self.calls = 0

    async def recognize(self, image: Image.Image) -> OCRResult:
        self.calls += 1
        return OCRResult("도로과장 송태아", 0.9, self.name)


async def test_caption_readings_are_reused_only_for_the_same_crop(  # type: ignore[no-untyped-def]
    meeting_video, tmp_path: Path
) -> None:
    store = DirCheckpoints(tmp_path / "checkpoints")

    async def run(options: MediaOptions) -> int:
        ocr = _StubOCR()
        await transcribe_media(
            meeting_video.video,
            workdir=tmp_path,
            stt=_stt(meeting_video),
            ocr=ocr,
            checkpoints=store,
            options=options,
        )
        return ocr.calls

    assert await run(OPTIONS) > 0
    assert await run(OPTIONS) == 0
    moved = MediaOptions(
        window_seconds=10.0, window_search_seconds=3.0, caption_region=(0.0, 0.0, 1.0, 0.25)
    )
    assert await run(moved) > 0
