"""A fresh, rule-chosen sample of council minutes for an extraction holdout.

The rule is fixed before anything is read and recorded in the sample's manifest: meetings held
in a date window, of the kinds a pattern names (standing committees, say), from councils not
excluded (the ones earlier evaluations or development already looked at), ordered by the
SHA-256 of their CLIK meeting id — an order nobody chooses — and at most ``per_council`` from
each council. The first ``meetings`` of that order are read in full and archived with their
hashes.

:func:`select_excerpts` then picks, again by rule and from the archived text alone, the speaker
turns that will be labelled: turns that state an amount of money or name a purchase-like act.
That selection keeps turns that turn out to hold no procurement (a 수당 amount, a 보조금), which
is what makes the negatives honest.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.parsing.chunking import split_turns
from app.sources.base import FetchWindow
from app.sources.clik import ClikMinutesAdapter, external_id

SCHEMA = "fresh-minutes-sample-v1"
AMOUNT = re.compile(r"\d[\d,.]*\s*(?:조|억|천만|백만|만)?\s*(?:천)?\s*원")
ACT = re.compile(r"구매|구입|설치|용역|구축|도입|교체|공사|발주|입찰|계약|임차")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _slug(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]+", "-", value).strip("-")[:60]


@dataclass(frozen=True, slots=True)
class Rule:
    since: str
    until: str
    meeting_pattern: str
    exclude: tuple[str, ...]
    meetings: int
    per_council: int = 1


async def sample_minutes(adapter: ClikMinutesAdapter, out_dir: Path, rule: Rule) -> dict[str, Any]:
    window = FetchWindow(date.fromisoformat(rule.since), date.fromisoformat(rule.until))
    listed = await adapter.list_meetings(window)
    pattern = re.compile(rule.meeting_pattern)
    eligible = []
    for key, rows in listed.items():
        first = rows[0]
        council = str(first.get("RASMBLY_NM") or "")
        if any(name in council for name in rule.exclude):
            continue
        if not pattern.search(str(first.get("MTGNM") or "")):
            continue
        ext = external_id(key)
        eligible.append((_sha(ext), ext, council, rows))
    eligible.sort()
    chosen: list[tuple[str, str, str, list[dict[str, Any]]]] = []
    per: dict[str, int] = {}
    for item in eligible:
        council = item[2]
        if per.get(council, 0) >= rule.per_council:
            continue
        per[council] = per.get(council, 0) + 1
        chosen.append(item)
        if len(chosen) >= rule.meetings:
            break

    sources_dir = out_dir / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    sources: list[dict[str, Any]] = []
    for order, ext, council, rows in chosen:
        record = await adapter.read_meeting(ext, rows)
        entry: dict[str, Any] = {"meeting": ext, "council": council, "order_sha256": order}
        if record is None or not record.content:
            sources.append(entry | {"status": "empty"})
            continue
        text = record.content.decode("utf-8")
        source_id = _slug(f"{council}-{record.structured.get('meeting')}-{record.published_at}")
        path = sources_dir / f"{source_id}.txt"
        path.write_text(text, encoding="utf-8")
        sources.append(
            entry
            | {
                "status": "archived",
                "id": source_id,
                "institution": council.removesuffix("의회").strip(),
                "title": record.title,
                "meeting_date": record.published_at.isoformat(),
                "url": record.url,
                "docid": record.structured.get("docid"),
                "path": f"sources/{path.name}",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "chars": len(text),
            }
        )
    manifest = {
        "schema": SCHEMA,
        "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": "국회도서관 지방의정포털(CLIK) Open API, minutes.do list + detail",
        "rule": {
            "since": rule.since,
            "until": rule.until,
            "meeting_pattern": rule.meeting_pattern,
            "exclude_councils_containing": list(rule.exclude),
            "order": "sha256(meeting id) ascending",
            "meetings": rule.meetings,
            "per_council": rule.per_council,
        },
        "listed_meetings": len(listed),
        "eligible_meetings": len(eligible),
        "sources": sources,
    }
    (out_dir / "sample.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return manifest


def select_excerpts(
    source_id: str, text: str, *, per_source: int, min_chars: int = 80, max_chars: int = 2500
) -> list[dict[str, Any]]:
    """Speaker turns that state an amount or name a purchase-like act, in an order fixed by the
    hash of (source, offset), at most ``per_source``; returned in document order."""
    picked = []
    for turn in split_turns(text):
        body = text[turn.start : turn.end]
        if not min_chars <= len(body) <= max_chars:
            continue
        if not (AMOUNT.search(body) or ACT.search(body)):
            continue
        picked.append((_sha(f"{source_id}:{turn.start}"), turn.start, body))
    picked.sort()
    return [
        {"source_id": source_id, "text_char_start": start, "text": body}
        for _, start, body in sorted(picked[:per_source], key=lambda p: p[1])
    ]
