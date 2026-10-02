"""Every meeting of a few named councils from CLIK, archived for the retrospective check
(``docs/retrospective-validation.md``) beyond 성남시.

1. :func:`find_councils` — CLIK has no council directory, so the national list is read newest
   first until each wanted name appears in a ``RASMBLY_NM``; its ``RASMBLY_ID`` is kept.
2. :func:`archive` — for each council, the meetings held in the window whose name matches the
   pattern are read once each (:meth:`ClikMinutesAdapter.read_meeting`, the live adapter's own
   call). The text goes to ``sources/<id>.txt`` and every other field of the record to
   ``manifest.json``. The manifest is written after each meeting, and meetings already archived
   are skipped, so a run the daily quota (1,000 calls) cuts short is finished by running it again.
3. :class:`ArchivedMinutes` — an adapter that yields those records again, so ``run_ingest``
   stores them exactly as the live adapter would have, without the API.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.sources.base import DocType, FetchWindow, RawRecord, parse_compact_date
from app.sources.clik import ClikMinutesAdapter, external_id, meeting_key
from app.sources.resilience import QuotaExhaustedError

SCHEMA = "council-minutes-archive-v1"
MANIFEST = "manifest.json"


def _slug(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]+", "-", value).strip("-")[:80]


def _load(out_dir: Path) -> dict[str, Any]:
    path = out_dir / MANIFEST
    if path.exists():
        manifest: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("schema") != SCHEMA:
            raise ValueError(f"{path}: not a {SCHEMA} manifest")
        return manifest
    return {"schema": SCHEMA, "councils": {}, "meetings": {}, "runs": []}


def _save(out_dir: Path, manifest: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(out_dir / MANIFEST)


async def find_councils(
    adapter: ClikMinutesAdapter, names: list[str], *, until: date, max_rows: int = 10_000
) -> dict[str, dict[str, str]]:
    """``{name: {"id", "council"}}`` for every name found in the first ``max_rows`` rows of the
    national list (newest first). A name contained in two councils' names is refused."""
    found: dict[str, dict[str, str]] = {}
    seen = 0
    # The adapter's own paging (no council filter, so every council's rows, newest first).
    async for row in adapter._list("", FetchWindow(date(2000, 1, 1), until)):
        seen += 1
        council = str(row.get("RASMBLY_NM") or "")
        council_id = str(row.get("RASMBLY_ID") or "")
        for name in names:
            if name not in council or not council_id:
                continue
            known = found.get(name)
            if known and known["id"] != council_id:
                raise ValueError(f"{name!r} matches both {known['council']} and {council}")
            found[name] = {"id": council_id, "council": council}
        if len(found) == len(names) or seen >= max_rows:
            break
    return found


async def archive(
    adapter: ClikMinutesAdapter,
    out_dir: Path,
    names: list[str],
    *,
    since: date,
    until: date,
    meeting_pattern: str,
    max_details: int | None = None,
    find_rows: int = 10_000,
) -> dict[str, Any]:
    """Archive (or go on archiving) every matching meeting of ``names`` held in the window."""
    manifest = _load(out_dir)
    rule = {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "meeting_pattern": meeting_pattern,
    }
    if manifest.get("rule", rule) != rule:
        raise ValueError(f"{out_dir} was archived under another rule: {manifest['rule']}")
    manifest["rule"] = rule
    manifest["source"] = "국회도서관 지방의정포털(CLIK) Open API, minutes.do list + detail"
    run: dict[str, Any] = {"started_at": datetime.now(UTC).isoformat(timespec="seconds")}
    manifest["runs"].append(run)
    details = 0
    stopped: str | None = None
    try:
        missing = [n for n in names if n not in manifest["councils"]]
        if missing:
            found = await find_councils(adapter, missing, until=until, max_rows=find_rows)
            manifest["councils"].update(found)
            run["not_found"] = [n for n in missing if n not in found]
        pattern = re.compile(meeting_pattern)
        window = FetchWindow(since, until)
        sources = out_dir / "sources"
        sources.mkdir(parents=True, exist_ok=True)
        for name in names:
            council = manifest["councils"].get(name)
            if council is None:
                continue
            meetings: dict[Any, list[dict[str, Any]]] = {}
            async for row in adapter._list(council["id"], window):
                if pattern.search(str(row.get("MTGNM") or "")):
                    meetings.setdefault(meeting_key(row), []).append(row)
            council["listed_meetings"] = len(meetings)
            for key in sorted(meetings, key=lambda k: (k[4], k)):
                ext = external_id(key)
                if ext in manifest["meetings"]:
                    continue
                if max_details is not None and details >= max_details:
                    stopped = "max_details"
                    break
                details += 1
                rec = await adapter.read_meeting(ext, meetings[key])
                entry: dict[str, Any] = {"council": name}
                if rec is None or not rec.content:
                    day = parse_compact_date(meetings[key][0].get("MTG_DE"))
                    entry |= {"status": "empty", "meeting_date": day.isoformat() if day else None}
                else:
                    path = sources / f"{_slug(ext)}.txt"
                    path.write_bytes(rec.content)
                    entry |= {
                        "status": "archived",
                        "title": rec.title,
                        "meeting_date": rec.published_at.isoformat(),
                        "publisher_raw": rec.publisher_raw,
                        "url": rec.url,
                        "mime": rec.mime,
                        "structured": rec.structured,
                        "path": f"sources/{path.name}",
                        "sha256": hashlib.sha256(rec.content).hexdigest(),
                    }
                manifest["meetings"][ext] = entry
                _save(out_dir, manifest)
            if stopped:
                break
    except QuotaExhaustedError:
        stopped = "quota"
    run |= {
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "detail_calls": details,
        "stopped": stopped,
    }
    complete = stopped is None and all(n in manifest["councils"] for n in names)
    manifest["complete"] = complete
    _save(out_dir, manifest)
    return manifest


class ArchivedMinutes:
    """The archived meetings as the records the live CLIK adapter yielded."""

    doc_type: DocType = "council_minutes"

    def __init__(self, out_dir: Path, *, key: str = "clik_minutes") -> None:
        self.key = key
        self.out_dir = out_dir
        self.manifest = _load(out_dir)

    async def fetch(self, window: FetchWindow) -> AsyncIterator[RawRecord]:
        for ext, entry in sorted(self.manifest["meetings"].items()):
            if entry.get("status") != "archived":
                continue
            day = date.fromisoformat(entry["meeting_date"])
            if not window.since <= day <= window.until:
                continue
            content = (self.out_dir / entry["path"]).read_bytes()
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise ValueError(f"{entry['path']} does not match its archived hash")
            yield RawRecord(
                external_id=ext,
                doc_type="council_minutes",
                title=entry["title"],
                published_at=day,
                mime=entry["mime"],
                publisher_raw=entry["publisher_raw"],
                url=entry["url"],
                content=content,
                structured=entry["structured"],
            )

    async def aclose(self) -> None:
        return None
