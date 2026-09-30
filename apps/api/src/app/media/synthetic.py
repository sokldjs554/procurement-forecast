"""A synthetic council meeting video: captions in a lower-third band, a tone where each sentence
is spoken, silence between them. Tests, the cross-language queue E2E and demos use it; the STT
side is a recorded transcript (``segments.json``) because a tone has no words.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.media.ffmpeg import MediaError, binary

# A 36-second committee exchange, as a broadcast shows it: (band, start, end, sentences).
MEETING: list[tuple[str, float, float, list[tuple[float, float, str]]]] = [
    (
        "조우현 의원",
        0.0,
        12.0,
        [
            (0.5, 5.0, "작년 여름에 우동 지하차도가 잠길 뻔했잖습니까."),
            (5.5, 9.5, "수위 센서 같은 거라도 달 계획이 있는지요."),
        ],
    ),
    (
        "도로과장 송태아",
        12.0,
        24.0,
        [
            (
                12.5,
                17.5,
                "침수 위험이 있는 지하차도 3곳에 수위계와 자동 차단시설을 설치하는 사업을 "
                "이번 제1회 추경에 2억 4천만원 편성했습니다.",
            ),
            (18.0, 21.5, "8월 말까지 설치를 마칠 계획입니다."),
        ],
    ),
    (
        "도시건설위원장 박재신",
        24.0,
        36.0,
        [
            (24.5, 30.0, "더 질의하실 위원님 안 계십니까?"),
            (30.5, 34.0, "이상으로 오늘 회의를 모두 마치겠습니다."),
        ],
    ),
]
FONT = Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf")


@dataclass(frozen=True)
class SyntheticMeeting:
    video: Path
    audio_only: Path
    segments_file: Path  # {"segments": [...]}, what FixtureSTT reads
    segments: list[dict[str, Any]]
    duration: float


def missing_tools() -> list[str]:
    missing = [b for b in ("ffmpeg", "ffprobe", "tesseract") if shutil.which(b) is None]
    return [*missing, str(FONT)] if not FONT.exists() else missing


def build_meeting(out_dir: Path) -> SyntheticMeeting:
    if missing := missing_tools():
        raise MediaError(f"the synthetic meeting needs {missing}")
    out_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = binary("ffmpeg")
    duration = MEETING[-1][2]
    speech = "+".join(f"between(t,{a},{b})" for *_, lines in MEETING for a, b, _ in lines)
    tone = f"aevalsrc='0.3*sin(2*PI*330*t)*({speech})':s=16000:d={duration}"
    draw = []
    for i, (band, start, end, _) in enumerate(MEETING):
        (out_dir / f"band{i}.txt").write_text(band, encoding="utf-8")
        draw.append(
            f"drawtext=fontfile={FONT}:textfile={out_dir / f'band{i}.txt'}:fontcolor=white:"
            f"fontsize=28:x=40:y=h*0.82:enable='between(t,{start},{end - 0.01})'"
        )
    video = out_dir / "meeting.mp4"
    subprocess.run(  # noqa: S603 - fixed arguments
        [
            *(ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"),
            *("-f", "lavfi", "-i", f"color=c=0x223344:s=640x360:r=25:d={duration}"),
            *("-f", "lavfi", "-i", tone),
            "-vf",
            ",".join(["drawbox=x=0:y=ih*0.78:w=iw:h=ih*0.16:color=black@0.85:t=fill", *draw]),
            *("-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-pix_fmt", "yuv420p"),
            *("-c:a", "aac", "-shortest", str(video)),
        ],
        check=True,
        capture_output=True,
    )
    audio_only = out_dir / "meeting.m4a"
    subprocess.run(  # noqa: S603 - fixed arguments
        [
            ffmpeg,
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-vn",
            "-c:a",
            "copy",
            str(audio_only),
        ],
        check=True,
        capture_output=True,
    )
    segments = [
        {"start": a, "end": b, "text": text} for *_, lines in MEETING for a, b, text in lines
    ]
    segments_file = out_dir / "segments.json"
    segments_file.write_text(
        json.dumps({"segments": segments}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return SyntheticMeeting(video, audio_only, segments_file, segments, duration)
