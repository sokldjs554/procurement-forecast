"""Test fixtures.

Unit tests need nothing but ffmpeg, tesseract and a Korean font for the media stage (skipped
locally without them, required in CI). Integration tests (``-m integration``) need PostgreSQL
with pgvector and Redis — ``docker compose up -d db redis`` locally, service containers in CI.
They run against a throwaway database (``<name>_test``) migrated with Alembic, so the migration
itself is tested.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

TEST_DB_URL = os.environ.get(
    "APP_TEST_DATABASE_URL", "postgresql+asyncpg://app:app@localhost:5432/app_test"
)
TEST_REDIS_URL = os.environ.get("APP_TEST_REDIS_URL", "redis://localhost:6379/15")

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_LOG_JSON", "false")
os.environ.setdefault("APP_LOG_LEVEL", "WARNING")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


async def _recreate_database() -> None:
    import asyncpg

    base, _, name = TEST_DB_URL.replace("+asyncpg", "").rpartition("/")
    conn = await asyncpg.connect(f"{base}/postgres")
    try:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1", name
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
async def migrated_db(tmp_path_factory: pytest.TempPathFactory) -> str:
    try:
        await _recreate_database()
    except (OSError, Exception) as exc:  # pragma: no cover - environment without Postgres
        pytest.skip(f"PostgreSQL not available: {exc}")
    os.environ["APP_DATABASE_URL"] = TEST_DB_URL
    os.environ["APP_REDIS_URL"] = TEST_REDIS_URL
    os.environ["APP_STORAGE_URL"] = f"file://{tmp_path_factory.mktemp('raw')}"
    from app.settings import get_settings

    get_settings.cache_clear()
    root = Path(__file__).resolve().parents[1]
    proc = await asyncio.create_subprocess_exec(
        "python",
        "-m",
        "alembic",
        "-c",
        str(root / "alembic.ini"),
        "upgrade",
        "head",
        cwd=root,
        env={**os.environ},
    )
    assert await proc.wait() == 0, "alembic upgrade failed"
    return TEST_DB_URL


@pytest.fixture(scope="session")
async def runtime(migrated_db: str) -> AsyncIterator[object]:
    from redis.asyncio import Redis

    from app.db.session import dispose_engine, get_engine
    from app.runtime import build_runtime
    from app.settings import get_settings

    settings = get_settings()
    redis = Redis.from_url(TEST_REDIS_URL)
    try:
        await redis.flushdb()
    except OSError as exc:  # pragma: no cover
        pytest.skip(f"Redis not available: {exc}")
    get_engine()
    rt = build_runtime(settings, redis=redis)
    yield rt
    await redis.aclose()
    await dispose_engine()


@pytest.fixture(scope="session")
async def demo_world(runtime: object) -> object:
    """Seed + run the pipeline once over a small synthetic world (no OCR, for speed)."""
    from datetime import date

    from sqlalchemy import update

    from app.db.models import Source
    from app.db.session import session_scope
    from app.demo.seed import run_demo_pipeline, seed_institutions, seed_sources, seed_tenants

    anchor = date(2026, 9, 25)
    async with session_scope() as s:
        await seed_institutions(s, runtime)  # type: ignore[arg-type]
        await seed_sources(s, runtime, anchor=anchor, seed=11, scale=0.6)  # type: ignore[arg-type]
        await s.flush()
        await s.execute(
            update(Source)
            .where(Source.key.like("fixture_%"))
            .values(
                config={
                    "anchor": anchor.isoformat(),
                    "seed": 11,
                    "scale": 0.6,
                    "scanned_ratio": 0.0,
                }
            )
        )
        await seed_tenants(s, runtime)  # type: ignore[arg-type]
    async with session_scope() as s:
        # No digest here: it would mark every org's recommendations as notified and leave
        # pending notifications in the shared test database (see test_pipeline for the digest).
        report = await run_demo_pipeline(s, runtime, anchor=anchor, digest=False)  # type: ignore[arg-type]
    return report


# A 36-second committee exchange, as a broadcast shows it: who is speaking in a lower-third
# band, their speech on the audio track (a tone where the words are, silence between them — the
# STT is a fixture, but silence detection and window cutting run on real audio).
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
                "이번 제1회 추경에 "
                "2억 4천만원 편성했습니다.",
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
class MeetingVideo:
    video: Path
    audio_only: Path
    segments: list[dict[str, Any]]
    duration: float


@pytest.fixture(scope="session")
def meeting_video(tmp_path_factory: pytest.TempPathFactory) -> MeetingVideo:
    missing = [b for b in ("ffmpeg", "ffprobe", "tesseract") if shutil.which(b) is None]
    if not FONT.exists():
        missing.append(str(FONT))
    if missing:
        if os.environ.get("CI"):
            pytest.fail(f"CI must install {missing} (see .github/workflows/ci.yml)")
        pytest.skip(f"media stage needs {missing}")
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    root = tmp_path_factory.mktemp("meeting")
    duration = MEETING[-1][2]
    speech = "+".join(f"between(t,{a},{b})" for *_, lines in MEETING for a, b, _ in lines)
    tone = f"aevalsrc='0.3*sin(2*PI*330*t)*({speech})':s=16000:d={duration}"
    draw = []
    for i, (band, start, end, _) in enumerate(MEETING):
        (root / f"band{i}.txt").write_text(band, encoding="utf-8")
        draw.append(
            f"drawtext=fontfile={FONT}:textfile={root / f'band{i}.txt'}:fontcolor=white:"
            f"fontsize=28:x=40:y=h*0.82:enable='between(t,{start},{end - 0.01})'"
        )
    video = root / "meeting.mp4"
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
    audio_only = root / "meeting.m4a"
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
    return MeetingVideo(video, audio_only, segments, duration)
