"""A checkpoint belongs to its source and inference settings, not a workdir name."""

from __future__ import annotations

import shutil
import struct
import wave
from pathlib import Path

import pytest

from app.media.ffmpeg import Window
from app.media.job import DirCheckpoints, MediaOptions, transcribe_media
from app.media.stt import FixtureSTT, Segment


@pytest.fixture
def source_audio(tmp_path):
    assert shutil.which("ffmpeg") and shutil.which("ffprobe"), "media tests require ffmpeg"
    path = tmp_path / "source.wav"
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\0\0" * (6 * 16000))
    return path


class LanguageSTT(FixtureSTT):
    async def transcribe(self, audio: Path, window: Window, *, language: str) -> list[Segment]:
        segments = await super().transcribe(audio, window, language=language)
        return [Segment(s.start, s.end, f"{language}: {s.text}") for s in segments]


async def test_language_change_cannot_return_the_previous_language(source_audio, tmp_path):
    provider = LanguageSTT([Segment(1, 5, "발언")])
    store = DirCheckpoints(tmp_path / "checkpoints")
    for language in ("ko", "en"):
        result = await transcribe_media(
            source_audio,
            workdir=tmp_path,
            stt=provider,
            ocr=None,
            checkpoints=store,
            options=MediaOptions(language=language),
        )
        assert result.segments and all(s.text.startswith(language + ":") for s in result.segments)


async def test_updated_recorded_transcript_invalidates_its_checkpoint(source_audio, tmp_path):
    store = DirCheckpoints(tmp_path / "checkpoints")
    for text in ("이전 판독", "교정한 판독"):
        result = await transcribe_media(
            source_audio,
            workdir=tmp_path,
            stt=FixtureSTT([Segment(1, 5, text)]),
            ocr=None,
            checkpoints=store,
        )
        assert [s.text for s in result.segments] == [text]


async def test_same_name_and_size_replaced_source_is_not_a_resume(source_audio, tmp_path):
    source = tmp_path / "meeting.wav"
    original = source_audio.read_bytes()
    store = DirCheckpoints(tmp_path / "checkpoints")
    provider = FixtureSTT([Segment(1, 5, "발언")])
    for marker in (b"a", b"b"):
        # Equal-sized valid WAVs: a JUNK chunk changes bytes without damaging audio.
        data = original + b"JUNK" + struct.pack("<I", 2) + marker + b"0"
        source.write_bytes(data[:4] + struct.pack("<I", len(data) - 8) + data[8:])
        result = await transcribe_media(
            source, workdir=tmp_path, stt=provider, ocr=None, checkpoints=store
        )
        assert result.resumed_windows == 0
