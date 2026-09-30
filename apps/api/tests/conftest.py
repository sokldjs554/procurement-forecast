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
from collections.abc import AsyncIterator
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


@pytest.fixture(scope="session")
def meeting_video(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """The synthetic meeting (app/media/synthetic.py): captions, pauses, recorded segments."""
    from app.media.synthetic import build_meeting, missing_tools

    if missing := missing_tools():
        if os.environ.get("CI"):
            pytest.fail(f"CI must install {missing} (see .github/workflows/ci.yml)")
        pytest.skip(f"media stage needs {missing}")
    return build_meeting(tmp_path_factory.mktemp("meeting"))
