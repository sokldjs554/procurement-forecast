"""Speech-to-text behind one interface.

* ``faster-whisper`` runs Whisper on the worker's CPU (int8): no per-minute bill, so what a
  meeting costs is compute time, which the job records per window.
* ``fixture`` serves recorded segments — tests, the demo world, and re-running a measured job
  without transcribing again.

A cloud engine (per-minute billed) plugs in behind the same ``transcribe``; its price per audio
minute is what the job multiplies by the seconds it actually sent, overlaps included.
Providers return times in seconds from the start of the whole recording, not the window.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from app.media.ffmpeg import MediaError, Window

# Words the decoder should prefer when the audio is ambiguous. Whisper conditions on this as if
# it were the preceding text; council vocabulary ("본예산", "추경", "반영하겠습니다") is rare in
# its training data and is exactly where a wrong word turns a commitment into noise.
DEFAULT_PROMPT = (
    "지방의회 본회의 회의록. 위원 질의와 집행부 답변. 본예산, 추경, 사업비 억 원, "
    "반영하겠습니다, 검토하겠습니다, 구청장, 국장, 과장."
)


@dataclass(frozen=True, slots=True)
class Segment:
    start: float
    end: float
    text: str
    confidence: float | None = None

    @property
    def midpoint(self) -> float:
        return (self.start + self.end) / 2

    def to_json(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "text": self.text,
            "confidence": self.confidence,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Segment:
        return cls(float(d["start"]), float(d["end"]), str(d["text"]), d.get("confidence"))


class STTProvider(Protocol):
    name: str
    model: str
    usd_per_audio_minute: float

    async def transcribe(self, audio: Path, window: Window, *, language: str) -> list[Segment]:
        """Segments of ``audio`` (the window's slice), timed from the start of the recording."""
        ...


class FasterWhisperSTT:
    name = "faster-whisper"
    usd_per_audio_minute = 0.0  # the worker's CPU; the job records compute seconds instead

    def __init__(
        self,
        model: str = "small",
        *,
        model_dir: str | None = None,
        compute_type: str = "int8",
        cpu_threads: int = 0,
        beam_size: int = 5,
        initial_prompt: str = DEFAULT_PROMPT,
    ) -> None:
        self.model = model
        self._model_dir = model_dir
        self._compute_type = compute_type
        self._cpu_threads = cpu_threads
        self._beam_size = beam_size
        self._prompt = initial_prompt
        self._loaded: Any = None

    def _load(self) -> Any:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # optional dependency
            raise MediaError("faster-whisper is not installed: uv sync --extra stt") from exc
        return WhisperModel(
            self._model_dir or self.model,
            device="cpu",
            compute_type=self._compute_type,
            cpu_threads=self._cpu_threads,
        )

    def _run(self, audio: Path, window: Window, language: str) -> list[Segment]:
        if self._loaded is None:
            self._loaded = self._load()
        segments, _info = self._loaded.transcribe(
            str(audio),
            language=language,
            beam_size=self._beam_size,
            vad_filter=True,  # silero VAD ships with the package; skips dead air between turns
            initial_prompt=self._prompt,
            # Carrying text across segments is what sends Whisper into repetition loops on long
            # formal speech; each segment stands on the prompt alone.
            condition_on_previous_text=False,
        )
        return [
            Segment(
                window.start + float(s.start),
                window.start + float(s.end),
                s.text.strip(),
                round(math.exp(float(s.avg_logprob)), 3),
            )
            for s in segments
            if s.text.strip()
        ]

    async def transcribe(self, audio: Path, window: Window, *, language: str) -> list[Segment]:
        return await asyncio.to_thread(self._run, audio, window, language)


class FixtureSTT:
    """Recorded segments, served for whichever window asks."""

    name = "fixture"
    model = "recorded"

    def __init__(self, segments: list[Segment], *, usd_per_audio_minute: float = 0.0) -> None:
        self._segments = sorted(segments, key=lambda s: s.start)
        self.usd_per_audio_minute = usd_per_audio_minute

    @classmethod
    def from_file(cls, path: Path) -> FixtureSTT:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data["segments"] if isinstance(data, dict) else data
        return cls([Segment.from_json(r) for r in rows])

    async def transcribe(self, audio: Path, window: Window, *, language: str) -> list[Segment]:
        return [s for s in self._segments if s.end > window.start and s.start < window.end]
