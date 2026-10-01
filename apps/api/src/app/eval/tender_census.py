"""Every official 나라장터 입찰공고 of a few institutions over a past period: the outcome side of
the retrospective check in ``docs/retrospective-validation.md`` (did a project caught in past
minutes or plans reach a tender, and how long after?).

Nothing here searches by name. Every 입찰공고 registered in the period is read, one week and one
업무구분 (용역·물품·공사) at a time, and the notices whose 공고기관 or 수요기관 name contains one of
the institution names are kept. Reading all of them is what lets the manifest say the census is
complete: a slice counts only when the rows read equal the provider's ``totalCount``; anything
else stays listed as incomplete, never as "no tender".

The run can stop (daily quota, time limit) and resume: finished slices are in the manifest and
are not read again. Officials' names, phone numbers and e-mail addresses are dropped from the
kept notices, as are the attachment links.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.sources import g2b
from app.sources.http import FatalSourceError, ResilientClient, TransientSourceError
from app.sources.resilience import QuotaExhaustedError

MANIFEST = "manifest.json"
NOTICES = "notices.jsonl.gz"
SCHEMA = "tender-census-v1"
BID_PATHS = g2b.OPERATIONS["g2b_bid"].paths

# Fields about people, and the per-notice attachment links (up to ten each, mostly signed URLs).
_DROPPED = re.compile(r"ofcl|exctv|tel|email|fax|SpecDoc|SpecFileNm|stdNtceDoc", re.IGNORECASE)


@dataclass(slots=True)
class Slice:
    path: str
    start: str
    end: str
    total: int | None = None  # provider totalCount
    read: int = 0
    kept: int = 0
    pages: int = 0
    error: str | None = None

    @property
    def complete(self) -> bool:
        return self.error is None and self.total is not None and self.read >= self.total


@dataclass(slots=True)
class Census:
    since: str
    until: str
    institutions: list[str]
    slices: list[Slice] = field(default_factory=list)
    stopped: str | None = None
    calls: int = 0
    seconds: float = 0.0

    def summary(self) -> dict[str, Any]:
        done = [s for s in self.slices if s.complete]
        return {
            "slices_expected": len(planned_slices(self.since, self.until)),
            "slices_complete": len(done),
            "rows_read": sum(s.read for s in self.slices),
            "notices_kept": sum(s.kept for s in self.slices),
            "incomplete": [
                {"path": s.path, "start": s.start, "end": s.end, "error": s.error}
                for s in self.slices
                if not s.complete
            ],
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "source": "조달청 나라장터 입찰공고정보서비스 (apis.data.go.kr/1230000/ad/BidPublicInfoService)",
            "query": "inqryDiv=1 (공고 등록일시), every row read; kept if 공고기관·수요기관 name contains an institution",
            "since": self.since,
            "until": self.until,
            "institutions": self.institutions,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "stopped": self.stopped,
            "calls": self.calls,
            "seconds": round(self.seconds, 1),
            "summary": self.summary(),
            "slices": [asdict(s) for s in self.slices],
        }


def planned_slices(since: str, until: str, window_days: int = 7) -> list[tuple[str, str, str]]:
    """``(path, start, end)`` for every week of the period and every 업무구분, in reading order."""
    start, last = date.fromisoformat(since), date.fromisoformat(until)
    out: list[tuple[str, str, str]] = []
    while start <= last:
        end = min(start + timedelta(days=window_days - 1), last)
        out += [(path, start.isoformat(), end.isoformat()) for path in BID_PATHS]
        start = end + timedelta(days=1)
    return out


def kept(item: dict[str, Any], institutions: list[str]) -> bool:
    names = f"{item.get('ntceInsttNm') or ''}\n{item.get('dminsttNm') or ''}"
    return any(name in names for name in institutions)


def slim(item: dict[str, Any], path: str) -> dict[str, Any]:
    out = {k: v for k, v in item.items() if v not in (None, "") and not _DROPPED.search(k)}
    out["_operation"] = path.rsplit("/", 1)[-1]
    return out


def load(out_dir: Path) -> Census | None:
    path = out_dir / MANIFEST
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Census(
        since=raw["since"],
        until=raw["until"],
        institutions=raw["institutions"],
        slices=[Slice(**s) for s in raw["slices"]],
        calls=raw.get("calls", 0),
        seconds=raw.get("seconds", 0.0),
    )


def _save(out_dir: Path, census: Census) -> None:
    tmp = out_dir / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(census.to_json(), ensure_ascii=False, indent=1) + "\n", "utf-8")
    tmp.replace(out_dir / MANIFEST)


def _open(out_dir: Path, since: str, until: str, institutions: list[str]) -> Census:
    out_dir.mkdir(parents=True, exist_ok=True)
    census = load(out_dir)
    if census is None:
        return Census(since=since, until=until, institutions=institutions)
    if (census.since, census.until, census.institutions) != (since, until, institutions):
        raise ValueError(
            f"{out_dir} holds a census of {census.since}..{census.until} for "
            f"{census.institutions}; use a new directory for a different one"
        )
    return census


async def run_census(
    client: ResilientClient,
    service_key: str,
    out_dir: Path,
    *,
    since: str,
    until: str,
    institutions: list[str],
    rows: int = 999,
    max_minutes: float | None = None,
    pause_seconds: float = 0.2,
) -> Census:
    """Read the slices not yet complete in ``out_dir`` and append the kept notices.

    A slice is read whole or not at all: its notices are appended only once every page came
    back, so a resumed run never writes a notice twice.
    """
    census = _open(out_dir, since, until, institutions)
    census.stopped = None
    done = {(s.path, s.start, s.end) for s in census.slices if s.complete}
    census.slices = [s for s in census.slices if s.complete]
    key = g2b.normalize_service_key(service_key)
    deadline = time.monotonic() + max_minutes * 60 if max_minutes else None
    started = time.monotonic()
    try:
        for path, start, end in planned_slices(since, until):
            if (path, start, end) in done:
                continue
            if deadline and time.monotonic() > deadline:
                census.stopped = "time limit"
                break
            current = Slice(path, start, end)
            census.slices.append(current)
            batch: list[dict[str, Any]] = []
            page = 1
            try:
                while True:
                    items, total = await g2b.fetch_page(
                        client,
                        key,
                        path,
                        date.fromisoformat(start),
                        date.fromisoformat(end),
                        page=page,
                        rows=rows,
                    )
                    census.calls += 1
                    current.pages += 1
                    current.read += len(items)
                    if page == 1:
                        current.total = total
                    batch += [slim(i, path) for i in items if kept(i, institutions)]
                    if page * rows >= total or not items:
                        break
                    page += 1
                    await asyncio.sleep(pause_seconds)
            except QuotaExhaustedError:
                current.error = "daily quota"
                census.stopped = "daily quota"
                break
            except FatalSourceError as exc:  # key or parameters: every other slice would fail too
                current.error = str(exc)[:300]
                census.stopped = "provider refused the request"
                break
            except TransientSourceError as exc:  # retried already; the next run reads it again
                current.error = str(exc)[:300]
            if current.error is None and current.total is not None and current.read < current.total:
                current.error = f"read {current.read} of {current.total}"
            if current.complete:
                current.kept = len(batch)
                with gzip.open(out_dir / NOTICES, "at", encoding="utf-8") as handle:
                    for row in batch:
                        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            _save(out_dir, census)
            await asyncio.sleep(pause_seconds)
    finally:
        census.seconds += time.monotonic() - started
        _save(out_dir, census)
    return census
