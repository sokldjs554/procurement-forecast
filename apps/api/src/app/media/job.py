"""One video → transcript, resumable and costed.

Every expensive step writes a checkpoint before the next one starts: the extracted audio, the
silence map, each STT window, the caption readings. A job killed at window 9 of 12 (deploy,
OOM, a spot instance taken back) re-runs from window 10 and pays for nothing twice. The
checkpoint store is an interface: a directory for the CLI, any store that writes a key
atomically for a queue worker.

Cost is counted per job, not per month: audio seconds sent to STT (overlaps included — they are
billed), STT and OCR compute seconds, ffmpeg seconds, and the provider's price per audio minute.
A resumed run reports what the whole result took, windows finished by the earlier attempt
included (their checkpoints keep their seconds); a call lost mid-window is not counted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.media.captions import Caption, CaptionStats, CaptionTrack, read_captions
from app.media.ffmpeg import (
    MediaInfo,
    Window,
    caption_frames,
    cut_audio,
    detect_silences,
    extract_audio,
    plan_windows,
    probe,
)
from app.media.stt import Segment, STTProvider
from app.media.transcript import Transcript, assemble, merge_segments
from app.parsing.ocr import OCREngine


class Checkpoints(Protocol):
    async def get(self, key: str) -> Any | None: ...

    async def put(self, key: str, value: Any) -> None: ...


class DirCheckpoints:
    """One JSON file per key under the job's work directory — the CLI's store."""

    def __init__(self, root: Path) -> None:
        self._root = root
        root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self._root / (key.replace(":", "_").replace("/", "_") + ".json")

    async def get(self, key: str) -> Any | None:
        path = self._path(key)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    async def put(self, key: str, value: Any) -> None:
        path = self._path(key)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)  # atomic: a crash mid-write never leaves a half checkpoint


class MemoryCheckpoints:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get(self, key: str) -> Any | None:
        return self.data.get(key)

    async def put(self, key: str, value: Any) -> None:
        self.data[key] = value


@dataclass(slots=True)
class MediaOptions:
    language: str = "ko"
    window_seconds: float = 600.0
    window_search_seconds: float = 30.0  # how far from the mark a pause may be to cut there
    caption_region: tuple[float, float, float, float] = (0.0, 0.70, 1.0, 0.30)
    caption_every_seconds: float = 2.0
    caption_scene: float = 0.12


@dataclass(slots=True)
class MediaCost:
    audio_seconds_sent: float = 0.0  # what an STT provider bills: every window, overlaps too
    stt_usd: float = 0.0
    stt_seconds: float = 0.0  # compute
    ocr_frames: int = 0
    ocr_seconds: float = 0.0
    ffmpeg_seconds: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {k: round(v, 4) if isinstance(v, float) else v for k, v in asdict(self).items()}


@dataclass(slots=True)
class MediaTranscript:
    info: MediaInfo
    windows: list[Window]
    segments: list[Segment]
    captions: list[Caption]
    caption_stats: CaptionStats
    transcript: Transcript
    cost: MediaCost
    stt_provider: str
    stt_model: str
    resumed_windows: int = 0
    notes: list[str] = field(default_factory=list)

    def media_json(self) -> dict[str, Any]:
        """What the document keeps about its video (``documents.structured['media']``). Only
        what the recording and the models determine: a wall-clock timing here would give every
        re-run a new content hash and re-process an unchanged transcript. Timings belong to the
        job (the CLI summary, the queue's job row)."""
        cost = self.cost.to_json()
        stats = self.caption_stats.to_json()
        return {
            "duration": round(self.info.duration, 2),
            "has_video": self.info.has_video,
            "size_bytes": self.info.size_bytes,
            "windows": [
                {"start": round(w.start, 2), "end": round(w.end, 2), "cut": w.cut}
                for w in self.windows
            ],
            "stt": {"provider": self.stt_provider, "model": self.stt_model},
            "captions": [c.to_json() for c in self.captions],
            "caption_stats": {k: stats[k] for k in ("frames", "ocr", "read")},
            "timeline": self.transcript.timeline,
            "turns": self.transcript.turns,
            "cost": {k: cost[k] for k in ("audio_seconds_sent", "stt_usd", "ocr_frames")},
            "notes": self.notes,
        }


Progress = Callable[[str, int, int], Awaitable[None]]


async def _noop(stage: str, done: int, total: int) -> None:
    return None


def _source_digest(src: Path) -> str:
    # Stream large media and keep disk I/O outside the event loop.
    with src.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


async def transcribe_media(
    src: Path,
    *,
    workdir: Path,
    stt: STTProvider,
    ocr: OCREngine | None,
    checkpoints: Checkpoints,
    options: MediaOptions | None = None,
    progress: Progress = _noop,
) -> MediaTranscript:
    opts = options or MediaOptions()
    cost = MediaCost()
    source_digest = await asyncio.to_thread(_source_digest, src)
    # A workdir is a location, not proof that its previous source is unchanged.
    # Versioning also prevents reuse of legacy checkpoints without a source identity.
    namespace = f"media-v2:{source_digest}:"
    workdir = workdir / source_digest
    workdir.mkdir(parents=True, exist_ok=True)
    info = await probe(src)
    if not info.has_audio:
        raise ValueError(f"{src.name} has no audio track")

    audio = workdir / "audio.flac"
    if await checkpoints.get(namespace + "audio") is None or not audio.exists():
        started = time.monotonic()
        await extract_audio(src, audio)
        cost.ffmpeg_seconds += time.monotonic() - started
        await checkpoints.put(namespace + "audio", {"bytes": audio.stat().st_size})
    await progress("audio", 1, 1)

    silences = await checkpoints.get(namespace + "silences")
    if silences is None:
        started = time.monotonic()
        silences = [list(s) for s in await detect_silences(audio)]
        cost.ffmpeg_seconds += time.monotonic() - started
        await checkpoints.put(namespace + "silences", silences)
    windows = plan_windows(
        info.duration,
        [(a, b) for a, b in silences],
        target=opts.window_seconds,
        search=opts.window_search_seconds,
    )

    parts: list[tuple[Window, list[Segment]]] = []
    resumed = 0
    for window in windows:
        # The window's geometry is part of the key: a re-plan (another window length) must not
        # reuse segments cut for different boundaries.
        parameters = json.dumps([stt.cache_key, opts.language, window.start, window.end])
        key = namespace + "stt:" + hashlib.sha256(parameters.encode()).hexdigest()
        cached = await checkpoints.get(key)
        if cached is None:
            piece = workdir / "windows" / f"w{window.index:04d}.flac"
            started = time.monotonic()
            await cut_audio(audio, piece, window)
            cost.ffmpeg_seconds += time.monotonic() - started
            started = time.monotonic()
            segments = await stt.transcribe(piece, window, language=opts.language)
            elapsed = time.monotonic() - started
            piece.unlink(missing_ok=True)
            cached = {"segments": [s.to_json() for s in segments], "seconds": round(elapsed, 3)}
            await checkpoints.put(key, cached)
        else:
            resumed += 1
        parts.append((window, [Segment.from_json(s) for s in cached["segments"]]))
        cost.audio_seconds_sent += window.duration
        cost.stt_seconds += float(cached["seconds"])
        await progress("stt", window.index + 1, len(windows))
    cost.stt_usd = cost.audio_seconds_sent / 60 * stt.usd_per_audio_minute

    captions: list[Caption] = []
    stats = CaptionStats()
    notes: list[str] = []
    if not info.has_video:
        notes.append("no video track: every speaker is unknown and goes to review")
    elif ocr is None:
        notes.append("no OCR engine: every speaker is unknown and goes to review")
    else:
        # Like a window's key: other crop, sampling or engine → other readings.
        region = ",".join(f"{v:g}" for v in opts.caption_region)
        parameters = json.dumps(
            [
                getattr(ocr, "cache_key", ocr.name),
                region,
                opts.caption_every_seconds,
                opts.caption_scene,
            ]
        )
        key = namespace + "captions:" + hashlib.sha256(parameters.encode()).hexdigest()
        cached_captions = await checkpoints.get(key)
        if cached_captions is None:
            started = time.monotonic()
            frames = await caption_frames(
                src,
                workdir / "frames",
                region=opts.caption_region,
                scene=opts.caption_scene,
                every=opts.caption_every_seconds,
            )
            cost.ffmpeg_seconds += time.monotonic() - started
            captions, stats = await read_captions(frames, ocr)
            for frame in frames:
                frame.path.unlink(missing_ok=True)
            cached_captions = {
                "captions": [c.to_json() for c in captions],
                "stats": stats.to_json(),
            }
            await checkpoints.put(key, cached_captions)
        captions = [Caption.from_json(c) for c in cached_captions["captions"]]
        stats = CaptionStats(**cached_captions["stats"])
        cost.ocr_frames = stats.ocr
        cost.ocr_seconds = stats.seconds
    await progress("captions", 1, 1)

    segments = merge_segments(parts)
    transcript = assemble(segments, CaptionTrack(captions))
    return MediaTranscript(
        info=info,
        windows=windows,
        segments=segments,
        captions=captions,
        caption_stats=stats,
        transcript=transcript,
        cost=cost,
        stt_provider=stt.name,
        stt_model=stt.model,
        resumed_windows=resumed,
        notes=notes,
    )
