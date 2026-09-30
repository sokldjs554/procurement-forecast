"""ffmpeg and ffprobe, run as subprocesses with timeouts.

Nothing here holds media in Python memory: audio and frames go to files in the job's work
directory, so a two-hour 720p meeting costs disk, not RAM. A killed or cancelled call kills its
ffmpeg too — a worker that shuts down mid-job must not leave a decoder running.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


class MediaError(RuntimeError):
    """ffmpeg or ffprobe failed (or is missing); the message carries the tail of stderr."""


@dataclass(frozen=True, slots=True)
class MediaInfo:
    duration: float
    has_audio: bool
    has_video: bool
    width: int | None
    height: int | None
    audio_codec: str | None
    video_codec: str | None
    size_bytes: int


@dataclass(frozen=True, slots=True)
class Window:
    """One STT call's slice of the audio. Segments whose midpoint falls in [keep_start, keep_end)
    belong to this window; a hard cut overlaps its neighbour, and the overlap is owned by one side
    only, so no word is transcribed twice in the merged transcript."""

    index: int
    start: float
    end: float
    keep_start: float
    keep_end: float
    cut: str  # how the window ends: "silence" | "hard" | "end"

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class Frame:
    t: float  # seconds from the start of the video
    path: Path


def binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise MediaError(f"{name} not found — install ffmpeg (apt-get install ffmpeg)")
    return path


async def run(args: list[str], *, max_seconds: float) -> tuple[bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), max_seconds)
    except TimeoutError:
        raise MediaError(f"{Path(args[0]).name} timed out after {max_seconds:.0f}s") from None
    finally:
        if proc.returncode is None:  # timed out or cancelled
            proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        tail = err.decode("utf-8", "replace").strip().splitlines()[-4:]
        raise MediaError(f"{Path(args[0]).name} exited {proc.returncode}: {' | '.join(tail)}")
    return out, err


async def probe(path: Path, *, max_seconds: float = 60.0) -> MediaInfo:
    out, _ = await run(
        [
            binary("ffprobe"),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        max_seconds=max_seconds,
    )
    data = json.loads(out or b"{}")
    streams = data.get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")  # cover art is not video
        ),
        None,
    )
    durations = [data.get("format", {}).get("duration")] + [s.get("duration") for s in streams]
    duration = max((float(d) for d in durations if d not in (None, "N/A")), default=0.0)
    if duration <= 0:
        raise MediaError(f"{path.name}: no duration — not a media file?")
    return MediaInfo(
        duration=duration,
        has_audio=audio is not None,
        has_video=video is not None,
        width=int(video["width"]) if video and video.get("width") else None,
        height=int(video["height"]) if video and video.get("height") else None,
        audio_codec=audio.get("codec_name") if audio else None,
        video_codec=video.get("codec_name") if video else None,
        size_bytes=(await asyncio.to_thread(path.stat)).st_size,
    )


async def extract_audio(
    src: Path, dst: Path, *, sample_rate: int = 16_000, max_seconds: float = 3600.0
) -> Path:
    """Mono 16 kHz FLAC: what every STT engine resamples to anyway, lossless, and about a tenth
    of the video's size — the only artefact kept once the video's retention runs out."""
    await asyncio.to_thread(dst.parent.mkdir, parents=True, exist_ok=True)
    await run(
        [
            binary("ffmpeg"),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "flac",
            str(dst),
        ],
        max_seconds=max_seconds,
    )
    return dst


_SILENCE_RE = re.compile(r"silence_(start|end): (-?\d+(?:\.\d+)?)")


async def detect_silences(
    audio: Path, *, noise_db: float = -35.0, min_seconds: float = 0.4, max_seconds: float = 1800.0
) -> list[tuple[float, float]]:
    """Pauses long enough to cut between words: [(start, end), …] in seconds."""
    _, err = await run(
        [
            binary("ffmpeg"),
            "-nostdin",
            "-hide_banner",
            "-i",
            str(audio),
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_seconds}",
            "-f",
            "null",
            "-",
        ],
        max_seconds=max_seconds,
    )
    silences: list[tuple[float, float]] = []
    start: float | None = None
    for kind, value in _SILENCE_RE.findall(err.decode("utf-8", "replace")):
        if kind == "start":
            start = max(0.0, float(value))
        elif start is not None:
            silences.append((start, float(value)))
            start = None
    return silences


def plan_windows(
    duration: float,
    silences: list[tuple[float, float]],
    *,
    target: float = 600.0,
    search: float = 30.0,
    overlap: float = 2.0,
) -> list[Window]:
    """Cut about every ``target`` seconds, in the middle of the longest pause within ``search``
    seconds of the mark, so no word is split between two STT calls. Where nobody pauses (a
    budget list read out in one breath) cut hard and let the neighbours overlap by ``overlap``
    seconds; each side owns half of the overlap."""
    windows: list[Window] = []
    start = keep_start = 0.0
    while duration - start > target + search:
        goal = start + target
        pauses = [
            (end - begin, (begin + end) / 2)
            for begin, end in silences
            if abs((begin + end) / 2 - goal) <= search and (begin + end) / 2 > start + 1.0
        ]
        if pauses:
            _, cut = max(pauses)
            windows.append(Window(len(windows), start, cut, keep_start, cut, "silence"))
            start = keep_start = cut
        else:
            windows.append(
                Window(len(windows), start, goal + overlap, keep_start, goal + overlap / 2, "hard")
            )
            start, keep_start = goal - overlap, goal + overlap / 2
    windows.append(Window(len(windows), start, duration, keep_start, duration, "end"))
    return windows


async def cut_audio(src: Path, dst: Path, window: Window, *, max_seconds: float = 600.0) -> Path:
    await asyncio.to_thread(dst.parent.mkdir, parents=True, exist_ok=True)
    await run(
        [
            binary("ffmpeg"),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{window.start:.3f}",
            "-t",
            f"{window.duration:.3f}",
            "-i",
            str(src),
            "-c:a",
            "flac",
            str(dst),
        ],
        max_seconds=max_seconds,
    )
    return dst


def _fresh_dir(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("f_*.png"):
        old.unlink()


def _frame_files(out_dir: Path) -> list[Path]:
    return sorted(out_dir.glob("f_*.png"))


_PTS_RE = re.compile(r"Parsed_showinfo.*?pts_time:\s*(\d+(?:\.\d+)?)")


async def caption_frames(
    src: Path,
    out_dir: Path,
    *,
    region: tuple[float, float, float, float] = (0.0, 0.70, 1.0, 0.30),
    scene: float = 0.12,
    every: float = 2.0,
    keyframes_only: bool = True,
    max_seconds: float = 3600.0,
) -> list[Frame]:
    """Frames of the lower third (where broadcasts print "도로과장 송태아"): on a scene cut and
    at least every ``every`` seconds. The scene score alone misses a caption swap on a static
    shot (0.04–0.06 in the test video, under the 0.12 used for cuts), hence the tick; OCR skips
    the repeats. Decoding only keyframes (typically every 1–2 s in a streaming encode) is enough
    for a caption that stays up for seconds, and keeps a two-hour meeting from costing a full
    decode."""
    x, y, w, h = region
    await asyncio.to_thread(_fresh_dir, out_dir)
    select = f"select='isnan(prev_selected_t)+gt(scene,{scene})+gte(t-prev_selected_t,{every})'"
    _, err = await run(
        [
            binary("ffmpeg"),
            "-nostdin",
            "-hide_banner",
            *(["-skip_frame", "nokey"] if keyframes_only else []),
            "-i",
            str(src),
            "-an",
            "-vf",
            f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},{select},showinfo",
            "-fps_mode",
            "vfr",
            str(out_dir / "f_%06d.png"),
        ],
        max_seconds=max_seconds,
    )
    times = [float(t) for t in _PTS_RE.findall(err.decode("utf-8", "replace"))]
    files = await asyncio.to_thread(_frame_files, out_dir)
    if len(times) != len(files):
        raise MediaError(f"frame timestamps ({len(times)}) do not match frames ({len(files)})")
    return [Frame(t, path) for t, path in zip(times, files, strict=True)]
