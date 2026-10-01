"""A fresh, rule-chosen sample of council minutes for an extraction holdout.

The rule is fixed before anything is read and recorded in the sample's manifest: meetings held
in a date window, of the kinds a pattern names (standing committees, say), from councils not
excluded (the ones earlier evaluations or development already looked at), ordered by the
SHA-256 of their CLIK meeting id — an order nobody chooses — and at most ``per_council`` from
each council. The first ``meetings`` of that order are read in full and archived with their
hashes.

:func:`select_excerpts` then picks, again by rule and from the archived text alone, the passages
that will be labelled: blocks of whole lines that state an amount of money or name a
purchase-like act. Blocks know nothing about speakers, so a council whose speaker layout the
product does not parse is sampled like any other. The selection keeps passages that turn out to
hold no procurement (a 수당 amount, a 보조금), which is what makes the negatives honest.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

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


def blocks(text: str, *, target: int = 1000, longest: int = 1800) -> list[tuple[int, int]]:
    """Contiguous runs of whole lines of about ``target`` characters, as ``(start, end)``.

    Councils write speakers in many ways ("○위원 김하늘", "위원장 김홍순", or the role and the name
    on lines of their own), so the unit of selection knows nothing about speakers: a block is
    just lines. A single line longer than ``longest`` is cut at sentence ends."""
    out: list[tuple[int, int]] = []
    start: int | None = None
    offset = 0
    for line in text.splitlines(keepends=True):
        end = offset + len(line.rstrip("\r\n"))
        if start is None:
            start = offset
        if end - start >= target:
            if end - start <= longest:
                out.append((start, end))
            else:
                out += _cut(text, start, end, target)
            start = None
        offset += len(line)
    if start is not None and text[start:offset].strip():
        out.append((start, len(text.rstrip("\r\n"))))
    return [(a, b) for a, b in out if text[a:b].strip()]


def _cut(text: str, start: int, end: int, target: int) -> list[tuple[int, int]]:
    pieces = []
    while end - start > target * 1.5:
        cut = text.rfind(". ", start, start + target)
        cut = cut + 1 if cut > start + target // 3 else start + target
        pieces.append((start, cut))
        start = cut + (1 if text[cut : cut + 1] == " " else 0)
    pieces.append((start, end))
    return pieces


def select_excerpts(
    source_id: str, text: str, *, per_source: int, min_chars: int = 80
) -> list[dict[str, Any]]:
    """Blocks (:func:`blocks`) that state an amount or name a purchase-like act, in an order
    fixed by the hash of (source, offset), at most ``per_source``; returned in document order."""
    picked = []
    for start, end in blocks(text):
        body = text[start:end]
        if len(body.strip()) < min_chars or not (AMOUNT.search(body) or ACT.search(body)):
            continue
        picked.append((_sha(f"{source_id}:{start}"), start, body))
    picked.sort()
    return [
        {"source_id": source_id, "text_char_start": start, "text": body}
        for _, start, body in sorted(picked[:per_source], key=lambda p: p[1])
    ]


def draft_cases(sample_dir: Path, *, per_source: int = 6, development: int = 5) -> dict[str, int]:
    """``cases-dev.draft.jsonl`` and ``cases-test.draft.jsonl``: the selected excerpts with an
    empty ``expected`` for the labeller. The first ``development`` archived meetings in the
    sample's hash order are for development; the rest are held out."""
    manifest = json.loads((sample_dir / "sample.json").read_text(encoding="utf-8"))
    archived = [s for s in manifest["sources"] if s["status"] == "archived"]
    counts: dict[str, int] = {}
    for split, sources in (("dev", archived[:development]), ("test", archived[development:])):
        rows = []
        for source in sources:
            # Bytes, not read_text: universal newlines would turn a source's \r\n into \n and
            # the excerpts would no longer be found in the archived file.
            text = (sample_dir / source["path"]).read_bytes().decode("utf-8")
            for n, excerpt in enumerate(
                select_excerpts(source["id"], text, per_source=per_source), 1
            ):
                rows.append(
                    {
                        "id": f"{split}-{source['id']}-{n}",
                        "source_id": source["id"],
                        "institution": source["institution"],
                        "doc_type": "council_minutes",
                        "date": source["meeting_date"],
                        "date_basis": "meeting_date",
                        "fiscal_year": None,
                        "locator": {"text_char_start": excerpt["text_char_start"]},
                        "text": excerpt["text"],
                        "expected": None,
                        "notes": "",
                    }
                )
        path = sample_dir / f"cases-{split}.draft.jsonl"
        path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
        )
        counts[split] = len(rows)
    return counts


def freeze(sample_dir: Path, split: str, *, label_origin: str) -> Path:
    """Write ``manifest-<split>.json`` (``source-holdout-v1``) over ``cases-<split>.jsonl``, once
    every excerpt has a label list. The hashes are what later runs are checked against."""
    cases_path = sample_dir / f"cases-{split}.jsonl"
    data = cases_path.read_bytes()
    cases = [json.loads(line) for line in data.splitlines() if line.strip()]
    if not cases or any(not isinstance(c.get("expected"), list) for c in cases):
        raise ValueError(f"{cases_path.name}: every excerpt needs an expected list, even []")
    sample = json.loads((sample_dir / "sample.json").read_text(encoding="utf-8"))
    used = {c["source_id"] for c in cases}
    out = sample_dir / f"manifest-{split}.json"
    if out.exists():
        raise ValueError(f"{out.name} exists; a frozen holdout is never rewritten")
    manifest = {
        "schema_version": "source-holdout-v1",
        "name": f"{sample_dir.name}-{split}",
        "frozen_before_scoring": True,
        "frozen_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "label_origin": label_origin,
        "cases_path": cases_path.name,
        "cases_sha256": hashlib.sha256(data).hexdigest(),
        "sources": [
            {
                "id": s["id"],
                "path": s["path"],
                "sha256": s["sha256"],
                "url": s["url"],
                "institution": s["institution"],
                "title": s["title"],
                "meeting_date": s["meeting_date"],
            }
            for s in sample["sources"]
            if s.get("id") in used
        ],
    }
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return out
