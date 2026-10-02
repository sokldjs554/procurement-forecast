"""Freeze every open forecast of the running deployment at once (``manage eval freeze-open``).

A forecast can be checked against the tenders that followed it only if it was written down,
with a known time, before those tenders appeared. This writes, in one run:

- ``open-forecasts.jsonl`` — one line per opportunity with no tender yet (status ``open`` and no
  ``bid_published_at``): the title, stage, window and probability a later check matches against;
- ``snapshots/<institution>.jsonl`` — :func:`export_forecast_snapshot` for every institution that
  holds one, the input ``scripts/longitudinal-evaluate.py`` takes;
- ``manifest.json`` — when, at which revision, and each file's SHA-256.

Each snapshot is its own read-only transaction, so run it while no pass writes (the workflow
shares the scheduled operations' concurrency group). Nothing is recomputed: the stored
predictions are copied as they stand.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import InstitutionRow, Opportunity
from app.eval.forecast_snapshot import SnapshotLimitError, export_forecast_snapshot
from app.settings import Settings

SCHEMA = "forecast-freeze-v1"
OPEN_RULE = "status = 'open' and bid_published_at is null"
_FIELDS = (
    "id",
    "institution_code",
    "title",
    "category",
    "stage",
    "best_commitment",
    "conversion_prob",
    "est_budget_krw",
    "bid_window_start",
    "bid_window_end",
    "first_seen_at",
    "last_signal_at",
    "signal_count",
    "keywords",
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _prepare(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    if any(out_dir.iterdir()):
        raise ValueError(f"{out_dir} is not empty: a freeze is never overwritten")
    (out_dir / "snapshots").mkdir()


def _write_lines(path: Path, rows: list[dict[str, Any]]) -> str:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    return _sha256(path)


def _facts(path: Path) -> tuple[str, int]:
    return _sha256(path), path.stat().st_size


def _plain(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


async def open_forecasts(session: AsyncSession) -> list[dict[str, Any]]:
    """Every opportunity with no tender yet, with its institution's name, by id."""
    rows = await session.execute(
        select(Opportunity, InstitutionRow.name)
        .join(InstitutionRow, InstitutionRow.code == Opportunity.institution_code)
        .where(Opportunity.status == "open", Opportunity.bid_published_at.is_(None))
        .order_by(Opportunity.id)
    )
    return [
        {name: _plain(getattr(opportunity, name)) for name in _FIELDS}
        | {"institution_name": institution}
        for opportunity, institution in rows.all()
    ]


async def freeze_open(
    sessions: Callable[[], AsyncSession],
    out_dir: Path,
    *,
    code_revision: str,
    settings: Settings,
    max_signals: int = 10000,
    max_institutions: int | None = None,
) -> dict[str, Any]:
    """Write the open forecasts, one snapshot per institution holding one, and the manifest."""
    await asyncio.to_thread(_prepare, out_dir)
    started = _now()
    async with sessions() as session, session.begin():
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        listed_at = _now()
        forecasts = await open_forecasts(session)
        totals = await session.execute(
            select(Opportunity.status, func.count()).group_by(Opportunity.status)
        )
        by_status: dict[str, int] = dict(totals.all())
    listing = out_dir / "open-forecasts.jsonl"
    listing_sha = await asyncio.to_thread(_write_lines, listing, forecasts)
    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    for row in forecasts:
        counts[row["institution_code"]] = counts.get(row["institution_code"], 0) + 1
        names[row["institution_code"]] = row["institution_name"]
    order = sorted(counts, key=lambda code: (-counts[code], code))
    if max_institutions is not None:
        order = order[:max_institutions]
    institutions: list[dict[str, Any]] = []
    snapshots = out_dir / "snapshots"
    for code in order:
        entry: dict[str, Any] = {"code": code, "name": names[code], "open": counts[code]}
        path = snapshots / f"{code}.jsonl"
        try:
            async with sessions() as session:
                report = await export_forecast_snapshot(
                    session,
                    path,
                    institution_code=code,
                    code_revision=code_revision,
                    max_signals=max_signals,
                    settings=settings,
                )
        except SnapshotLimitError:
            entry |= {"status": "skipped", "reason": f"more than {max_signals} signals"}
        else:
            sha, size = await asyncio.to_thread(_facts, path)
            entry |= {
                "status": "frozen",
                "path": f"snapshots/{path.name}",
                "sha256": sha,
                "bytes": size,
                "captured_at": report.captured_at,
                "counts": report.counts,
            }
        institutions.append(entry)
    manifest = {
        "schema": SCHEMA,
        "started_at": started,
        "open_forecasts_listed_at": listed_at,
        "finished_at": _now(),
        "code_revision": code_revision,
        "open_rule": OPEN_RULE,
        "opportunities_by_status": by_status,
        "open_forecasts": {
            "path": listing.name,
            "count": len(forecasts),
            "institutions": len(counts),
            "sha256": listing_sha,
        },
        "institutions": institutions,
    }
    await asyncio.to_thread(
        (out_dir / "manifest.json").write_text,
        json.dumps(manifest, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    return manifest
