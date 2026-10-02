"""``manage eval freeze-open``: every opportunity with no tender yet is listed and frozen per
institution with its hash; tendered ones are left out and a freeze is never overwritten."""

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import delete

from app.db.models import InstitutionRow, Opportunity
from app.db.session import get_sessionmaker
from app.eval.forecast_freeze import freeze_open
from app.settings import Settings

CODES = ("LG-FREEZE-A", "LG-FREEZE-B")


@pytest.fixture
async def freeze_world(migrated_db):  # type: ignore[no-untyped-def]
    async with get_sessionmaker()() as session:
        for code, name in zip(CODES, ("가시", "나시"), strict=True):
            session.add(
                InstitutionRow(
                    code=code, name=name, kind="local_gov", sido="경기도", region_code="41999"
                )
            )
        rows = [
            Opportunity(
                institution_code=CODES[0],
                title="스마트쉘터 설치",
                category="smart_city",
                stage="council_mention",
                status="open",
                first_seen_at=date(2026, 9, 1),
                last_signal_at=date(2026, 9, 1),
                conversion_prob=0.4,
                signal_count=1,
            ),
            Opportunity(
                institution_code=CODES[0],
                title="도서관 리모델링",
                category="construction",
                stage="budget_line",
                status="open",
                first_seen_at=date(2026, 9, 2),
                last_signal_at=date(2026, 9, 2),
                bid_window_start=date(2027, 1, 1),
                bid_window_end=date(2027, 6, 30),
                conversion_prob=0.6,
                signal_count=2,
            ),
            Opportunity(
                institution_code=CODES[1],
                title="공원 조성",
                category="construction",
                stage="budget_line",
                status="open",
                first_seen_at=date(2026, 9, 3),
                last_signal_at=date(2026, 9, 3),
                conversion_prob=0.5,
                signal_count=1,
            ),
            Opportunity(  # already tendered: not a forecast any more
                institution_code=CODES[1],
                title="도로 포장",
                category="construction",
                stage="bid_notice",
                status="bid_open",
                first_seen_at=date(2026, 9, 4),
                last_signal_at=date(2026, 9, 20),
                bid_published_at=date(2026, 9, 20),
                conversion_prob=1.0,
                signal_count=1,
            ),
        ]
        session.add_all(rows)
        await session.commit()
        ids = [row.id for row in rows]
    try:
        yield ids
    finally:
        async with get_sessionmaker()() as session:
            await session.execute(delete(Opportunity).where(Opportunity.id.in_(ids)))
            await session.execute(delete(InstitutionRow).where(InstitutionRow.code.in_(CODES)))
            await session.commit()


async def test_open_forecasts_are_listed_and_frozen_per_institution(freeze_world, tmp_path: Path):  # type: ignore[no-untyped-def]
    out = tmp_path / "freeze"
    manifest = await freeze_open(
        get_sessionmaker(), out, code_revision="abc1234", settings=Settings(env="test")
    )

    listed = [
        json.loads(line)
        for line in (out / "open-forecasts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    mine = [row for row in listed if row["institution_code"] in CODES]
    assert [row["title"] for row in mine] == ["스마트쉘터 설치", "도서관 리모델링", "공원 조성"]
    assert mine[1]["bid_window_start"] == "2027-01-01" and mine[1]["institution_name"] == "가시"

    by_code = {entry["code"]: entry for entry in manifest["institutions"]}
    assert by_code[CODES[0]]["open"] == 2 and by_code[CODES[1]]["open"] == 1
    for code in CODES:
        entry = by_code[code]
        assert entry["status"] == "frozen"
        body = (out / entry["path"]).read_bytes()
        assert hashlib.sha256(body).hexdigest() == entry["sha256"]
        assert json.loads(body.splitlines()[0])["institution_code"] == code
    # the more open forecasts, the earlier
    codes = [entry["code"] for entry in manifest["institutions"]]
    assert codes.index(CODES[0]) < codes.index(CODES[1])
    assert (
        manifest["open_forecasts"]["sha256"]
        == hashlib.sha256((out / "open-forecasts.jsonl").read_bytes()).hexdigest()
    )
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8")) == manifest


async def test_a_freeze_is_never_overwritten(freeze_world, tmp_path: Path):  # type: ignore[no-untyped-def]
    out = tmp_path / "freeze"
    await freeze_open(
        get_sessionmaker(), out, code_revision="abc1234", settings=Settings(env="test")
    )
    with pytest.raises(ValueError, match="not empty"):
        await freeze_open(
            get_sessionmaker(), out, code_revision="abc1234", settings=Settings(env="test")
        )
