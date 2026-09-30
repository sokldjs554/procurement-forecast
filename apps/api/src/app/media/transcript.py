"""Windows of STT output + a caption track → one minutes-shaped transcript and its timeline.

The text is what a clerk would type: one ``○role name`` line per speaker turn, the speech after
it. Nothing downstream needs to know it came from audio. The timeline keeps, for every STT
segment, its character span in that text and its seconds in the video, so an evidence span the
verifier locates (characters) becomes a moment to play or a clip to cut (seconds).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.media.captions import UNKNOWN, CaptionTrack, Speaker
from app.media.ffmpeg import Window
from app.media.stt import Segment


def merge_segments(parts: list[tuple[Window, list[Segment]]]) -> list[Segment]:
    """Each window keeps the segments it owns (midpoint in [keep_start, keep_end)), so words in
    a hard cut's overlap appear once. Empty and whitespace-only segments are dropped."""
    merged: list[Segment] = []
    for i, (window, segments) in enumerate(parts):
        last = i == len(parts) - 1
        for s in segments:
            m = s.midpoint
            owned = window.keep_start <= m < window.keep_end or (last and m >= window.keep_start)
            if owned and s.text.strip():
                merged.append(s)
    return sorted(merged, key=lambda s: (s.start, s.end))


@dataclass(slots=True)
class Transcript:
    text: str
    timeline: list[dict[str, Any]] = field(default_factory=list)  # per segment
    turns: list[dict[str, Any]] = field(default_factory=list)  # per speaker line

    def time_range(self, start: int, end: int) -> tuple[float, float] | None:
        """Seconds covered by the characters [start, end) — an evidence span → a clip."""
        hits = [e for e in self.timeline if e["start"] < end and start < e["end"]]
        if not hits:
            return None
        return min(e["t0"] for e in hits), max(e["t1"] for e in hits)


def assemble(segments: list[Segment], track: CaptionTrack | None) -> Transcript:
    text = ""
    timeline: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    current: Speaker | None = None
    for seg in segments:
        speaker = (track.speaker_at(seg.midpoint) if track else None) or UNKNOWN
        if speaker != current:
            if turns:
                turns[-1]["end"] = len(text)
                text += "\n"
            turns.append(
                {
                    "start": len(text),
                    "end": None,
                    "role": speaker.role,
                    "name": speaker.name,
                    "t0": round(seg.start, 2),
                }
            )
            text += speaker.header() + " "
            current = speaker
        else:
            text += " "
        begin = len(text)
        text += seg.text.strip()
        timeline.append(
            {"start": begin, "end": len(text), "t0": round(seg.start, 2), "t1": round(seg.end, 2)}
        )
        turns[-1]["t1"] = round(seg.end, 2)
    if turns:
        turns[-1]["end"] = len(text)
        text += "\n"
    return Transcript(text, timeline, turns)
