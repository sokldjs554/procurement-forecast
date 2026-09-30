"""Who is speaking: the lower-third caption ("도로과장 송태아", "조우현 의원") read with OCR.

Speaker identity decides whether a sentence is a commitment at all: "반영하겠습니다" from 도로과장
is an executive answer, the same words from a member are a demand (58 of 97 signals were exactly
that mistake on real text minutes, docs/real-data-minutes.md). Audio alone does not say who is
talking; the broadcast prints it. So the caption band is sampled every keyframe or two, OCR'd
only when it changed since the last frame read, parsed into (role, name), and carried forward
until the next caption — a speaker keeps talking after the band clears. Frames nobody can read leave the speaker unknown, and an
unknown speaker is never an executive (grounding sends those signals to review).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageChops, ImageOps

from app.domain.speakers import OFFICIAL_ENDINGS
from app.media.ffmpeg import Frame
from app.parsing.ocr import OCREngine

MEMBER_ROLES = ("위원", "의원")
CHAIR_ROLES = ("위원장", "부의장", "의장")
_ENDINGS = "|".join(sorted(OFFICIAL_ENDINGS, key=len, reverse=True))
# Words a band prints that look like a 2–4 syllable name but are not one.
_NOT_NAMES = frozenset(
    (
        *("위원회", "본회의", "회의", "질의", "답변", "예산", "결산", "안건", "보고", "의사"),
        *("일정", "개의", "산회", "정회", "속개", "제안", "설명", "심사", "토론", "의결"),
        *("표결", "생중계", "다시보기", "인터넷", "방송", "의회", "시의회", "구의회"),
    )
)

# A committee chair's band often names the committee ("도시건설위원장 조우현"); the role kept is
# the bare 위원장/의장, which is what text minutes print and what the chunker treats as a chair.
# Checked before the official endings: "…위원장" also ends in 원장.
_CHAIR_FIRST = re.compile(
    r"(?<![가-힣])[가-힣]{0,8}?(위원장|부의장|의장)\s+([가-힣]{2,4})(?![가-힣])"
)
_NAME_CHAIR = re.compile(
    r"(?<![가-힣])([가-힣]{2,4})\s+[가-힣]{0,8}?(위원장|부의장|의장)(?![가-힣])"
)
_OFFICIAL_FIRST = re.compile(
    rf"(?<![가-힣])([가-힣]{{0,12}}(?:{_ENDINGS}))\s+([가-힣]{{2,4}})(?![가-힣])"
)
_NAME_OFFICIAL = re.compile(
    rf"(?<![가-힣])([가-힣]{{2,4}})\s+([가-힣]{{0,12}}(?:{_ENDINGS}))(?![가-힣])"
)
# OCR sometimes loses the space between role and name; a Korean name is three syllables far
# more often than not, and only that reading is trusted when glued.
_OFFICIAL_GLUED = re.compile(
    rf"(?<![가-힣])([가-힣]{{0,12}}(?:{_ENDINGS}))([가-힣]{{3}})(?![가-힣])"
)
_STAFF_FIRST = re.compile(r"(?<![가-힣])([가-힣]{1,6}위원)\s+([가-힣]{2,4})(?![가-힣])")
_MEMBER = re.compile(r"(?<![가-힣])([가-힣]{2,4})\s?(위원|의원)(?![가-힣])")
_STAFF_PREFIXES = ("전문", "수석", "상임", "운영", "간사", "소위")


@dataclass(frozen=True, slots=True)
class Speaker:
    role: str
    name: str

    @property
    def is_member(self) -> bool:
        return self.role in MEMBER_ROLES

    @property
    def is_chair(self) -> bool:
        return self.role in CHAIR_ROLES

    def header(self) -> str:
        """The speaker line as text minutes print it, so ``split_turns`` reads it back."""
        return f"○{self.name} {self.role}" if self.is_member else f"○{self.role} {self.name}"


UNKNOWN = Speaker("발언자", "미상")


def _name_ok(name: str) -> bool:
    return (
        2 <= len(name) <= 4
        and name not in _NOT_NAMES
        and not name.endswith((*MEMBER_ROLES, *CHAIR_ROLES, *OFFICIAL_ENDINGS))
    )


def _normalize(text: str) -> str:
    """Hangul and spaces only; a name OCR'd one syllable at a time ('송 태 아') joined back."""
    tokens = re.sub(r"[^가-힣\s]", " ", text).split()
    out: list[str] = []
    run: list[str] = []
    for token in [*tokens, ""]:
        if len(token) == 1:
            run.append(token)
            continue
        if 2 <= len(run) <= 4:
            out.append("".join(run))
        else:
            out.extend(run)
        run = []
        if token:
            out.append(token)
    return " ".join(out)


def parse_caption(text: str) -> Speaker | None:
    """'도로과장 송태아' → Speaker('도로과장', '송태아'); '조우현 의원' and '조우현의원' →
    Speaker('의원', '조우현'); '도시건설위원장 조우현' → Speaker('위원장', '조우현'). A band that
    prints the department too ('교통도로국 도로과장 송태아') keeps the role word next to the
    name. ``None`` when the band holds no role–name pair (an agenda title, a station logo)."""
    flat = _normalize(text)
    if not flat:
        return None
    for pattern, role_group, name_group in (
        (_CHAIR_FIRST, 1, 2),
        (_NAME_CHAIR, 2, 1),
        (_OFFICIAL_FIRST, 1, 2),
        (_NAME_OFFICIAL, 2, 1),
        (_STAFF_FIRST, 1, 2),
    ):
        for m in pattern.finditer(flat):
            if _name_ok(name := m.group(name_group)):
                return Speaker(m.group(role_group), name)
    for m in _MEMBER.finditer(flat):
        name, role = m.group(1), m.group(2)
        if _name_ok(name) and not name.startswith(_STAFF_PREFIXES):
            return Speaker(role, name)
    for m in _OFFICIAL_GLUED.finditer(flat):
        if _name_ok(name := m.group(2)):
            return Speaker(m.group(1), name)
    return None


@dataclass(frozen=True, slots=True)
class Caption:
    start: float
    end: float
    speaker: Speaker
    text: str
    confidence: float

    def to_json(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "role": self.speaker.role,
            "name": self.speaker.name,
            "text": self.text,
            "confidence": self.confidence,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Caption:
        return cls(d["start"], d["end"], Speaker(d["role"], d["name"]), d["text"], d["confidence"])


@dataclass(slots=True)
class CaptionStats:
    frames: int = 0
    ocr: int = 0  # frames actually sent to OCR; the rest repeated the band before them
    read: int = 0  # frames whose band parsed as a speaker
    seconds: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "frames": self.frames,
            "ocr": self.ocr,
            "read": self.read,
            "seconds": round(self.seconds, 2),
        }


def prepare_band(image: Image.Image) -> Image.Image:
    """Captions are small light text on a dark band (or the reverse). Upscale so strokes survive
    binarisation, and make the text dark on light, which is what tesseract is trained on."""
    gray = ImageOps.autocontrast(ImageOps.grayscale(image), cutoff=1)
    gray = gray.resize((gray.width * 2, gray.height * 2), Image.Resampling.LANCZOS)
    mean = sum(i * n for i, n in enumerate(gray.histogram())) / max(1, gray.width * gray.height)
    return ImageOps.invert(gray) if mean < 128 else gray


def band_signature(image: Image.Image) -> Image.Image:
    """The band averaged down to 64×16 cells: compression noise vanishes, a changed name does
    not (a new caption moves some cell by ~90 levels; a static band by 0–1)."""
    return ImageOps.grayscale(image).resize((64, 16), Image.Resampling.BOX)


def band_changed(a: Image.Image | None, b: Image.Image, *, threshold: int = 24) -> bool:
    if a is None:
        return True
    return max(ImageChops.difference(a, b).tobytes(), default=0) >= threshold


def merge_readings(readings: list[tuple[float, Speaker | None, str, float]]) -> list[Caption]:
    """Consecutive frames reading the same speaker become one caption. A frame with no speaker
    closes the caption (the band cleared) but does not change who is talking — that is the
    track's job."""
    captions: list[Caption] = []
    current: tuple[float, float, Speaker, str, float] | None = None
    for t, speaker, text, conf in sorted(readings, key=lambda r: r[0]):
        if current and speaker == current[2]:
            current = (current[0], t, speaker, current[3], max(current[4], conf))
            continue
        if current:
            captions.append(Caption(current[0], t, current[2], current[3], current[4]))
            current = None
        if speaker:
            current = (t, t, speaker, text, conf)
    if current:
        captions.append(Caption(*current))
    return captions


async def read_captions(
    frames: list[Frame], ocr: OCREngine, *, min_confidence: float = 0.45
) -> tuple[list[Caption], CaptionStats]:
    """Frames are sampled densely (a caption must not start seconds late); OCR runs only on
    frames whose band differs from the last one read, which on a meeting — one caption for
    minutes at a time — is a small fraction of them."""
    stats = CaptionStats(frames=len(frames))
    started = time.monotonic()
    readings: list[tuple[float, Speaker | None, str, float]] = []
    last: tuple[Image.Image, Speaker | None, str, float] | None = None
    for frame in frames:
        with Image.open(frame.path) as img:
            signature = band_signature(img)
            if last is not None and not band_changed(last[0], signature):
                _, speaker, text, conf = last
            else:
                result = await ocr.recognize(prepare_band(img))
                stats.ocr += 1
                conf = result.confidence
                speaker = parse_caption(result.text) if conf >= min_confidence else None
                text = " ".join(result.text.split())
                last = (signature, speaker, text, conf)
        if speaker:
            stats.read += 1
        readings.append((frame.t, speaker, text, conf))
    stats.seconds = time.monotonic() - started
    return merge_readings(readings), stats


class CaptionTrack:
    """Speaker at a moment: the latest caption that started at or before it. ``lead`` absorbs
    the second or two a broadcast takes to put the band up after someone starts talking."""

    def __init__(self, captions: list[Caption], *, lead: float = 1.5) -> None:
        self._captions = sorted(captions, key=lambda c: c.start)
        self._lead = lead

    def speaker_at(self, t: float) -> Speaker | None:
        found: Speaker | None = None
        for caption in self._captions:
            if caption.start > t + self._lead:
                break
            found = caption.speaker
        return found
