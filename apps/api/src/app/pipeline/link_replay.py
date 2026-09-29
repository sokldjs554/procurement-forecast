"""Link real signals again without fetching or extracting: ``manage link export`` / ``replay``.

A live measurement of the linker costs a container, the sites' patience and forty minutes per
variant (docs/real-data-minutes.md §9), yet linking only reads the signals. ``export_signals``
writes them from a real run — without embeddings, which are recomputed from the same text —
together with the opportunity each one joined there. ``replay_links`` loads such a file into a
seeded database with no documents, and links the signals in the order ``process_pending`` would
have: documents by publication, signals as they were created, in slices of 2,000. A change to
``link.py`` can then be measured on the same real rows in seconds, and checked against the run
it came from.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, DocumentChunk, OpportunitySignal, Signal, Source
from app.domain.synonyms import canonicalize
from app.pipeline.link import link_signals
from app.runtime import Runtime

REPLAY_SOURCE = "link-replay"
# backfill.process_pending links the signals of a run in slices of this many, each in date order.
LINK_SLICE = 2000
_DOC_KEYS = ("fiscal_year", "budget_kind", "meeting_date", "published_from")
_SIGNAL_FIELDS = (
    "stage",
    "institution_code",
    "speaker_institution_code",
    "department",
    "title",
    "summary",
    "category",
    "keywords",
    "budget_krw",
    "expected_year",
    "expected_half",
    "commitment",
    "procurement_type",
    "confidence",
    "verdict",
    "extractor",
    "external_refs",
)


async def export_signals(
    session: AsyncSession,
    path: Path,
    *,
    doc_types: list[str],
    source_keys: list[str] | None = None,
) -> int:
    """Every signal of these document types (from these sources, when given), in creation
    order, with its document, its chunk's labels and the opportunity it joined (``null`` when
    it joined none)."""
    stmt = (
        select(Signal, Document, DocumentChunk.labels, OpportunitySignal.opportunity_id)
        .join(Document, Document.id == Signal.document_id)
        .outerjoin(DocumentChunk, DocumentChunk.id == Signal.chunk_id)
        .outerjoin(OpportunitySignal, OpportunitySignal.signal_id == Signal.id)
        .where(Document.doc_type.in_(doc_types))
        .order_by(Signal.id)
    )
    if source_keys:
        stmt = stmt.join(Source, Source.id == Document.source_id).where(Source.key.in_(source_keys))
    rows = (await session.execute(stmt)).all()
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for signal, doc, labels, opportunity_id in rows:
            record = {
                "key": signal.dedupe_key,
                "document": {
                    "external_id": doc.external_id,
                    "doc_type": doc.doc_type,
                    "title": doc.title,
                    "publisher_raw": doc.publisher_raw,
                    "institution_code": doc.institution_code,
                    "published_at": doc.published_at.isoformat(),
                    "structured": {k: doc.structured[k] for k in _DOC_KEYS if k in doc.structured},
                },
                "labels": list(labels or []),
                "observed_at": signal.observed_at.isoformat(),
                "opportunity": opportunity_id,
            } | {name: getattr(signal, name) for name in _SIGNAL_FIELDS}
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return len(rows)


def load_export(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@dataclass(slots=True)
class ReplayResult:
    signals: int
    linked: int
    # Each opportunity as the sorted keys of its signals; sorted, so two results compare as lists.
    groups: list[list[str]] = field(default_factory=list)
    # Compared with the opportunities recorded in the file (None when it recorded none).
    same_as_recorded: int | None = None
    recorded: int | None = None

    def summary(self) -> dict[str, Any]:
        sizes = Counter(len(g) for g in self.groups)
        out: dict[str, Any] = {
            "signals": self.signals,
            "linked": self.linked,
            "opportunities": len(self.groups),
            "with_more_than_one_signal": sum(n for size, n in sizes.items() if size > 1),
        }
        if self.recorded is not None:
            out["recorded_opportunities"] = self.recorded
            out["same_as_recorded"] = self.same_as_recorded
        return out


def recorded_groups(records: list[dict[str, Any]]) -> list[list[str]]:
    by_opp: dict[int, list[str]] = defaultdict(list)
    for r in records:
        if r.get("opportunity") is not None:
            by_opp[r["opportunity"]].append(r["key"])
    return sorted(sorted(keys) for keys in by_opp.values())


async def _source(session: AsyncSession) -> Source:
    source = await session.scalar(select(Source).where(Source.key == REPLAY_SOURCE))
    if source is None:
        source = Source(key=REPLAY_SOURCE, name="Link replay", adapter="replay", enabled=False)
        session.add(source)
        await session.flush()
    return source


async def replay_links(
    session: AsyncSession,
    runtime: Runtime,
    records: list[dict[str, Any]],
    *,
    first: list[str] | None = None,
    today: date,
) -> ReplayResult:
    """Load ``records`` (from ``export_signals``) and link them. ``first`` names document types
    processed and linked in a run of their own before the rest — the "budget books first" order
    of docs/real-data-minutes.md §8.1 — otherwise all go in one run, as one ``pipeline run``
    over everything pending would. Expects a seeded database with no opportunities yet."""
    source = await _source(session)
    documents: dict[tuple[str, str], Document] = {}
    chunks: dict[tuple[str, str, tuple[str, ...]], DocumentChunk] = {}
    for r in records:
        d = r["document"]
        dkey = (d["doc_type"], d["external_id"])
        if dkey not in documents:
            doc = Document(
                source_id=source.id,
                external_id=d["external_id"],
                doc_type=d["doc_type"],
                title=d["title"],
                publisher_raw=d["publisher_raw"],
                institution_code=d["institution_code"],
                published_at=date.fromisoformat(d["published_at"]),
                content_hash=hashlib.sha256("\0".join(dkey).encode()).hexdigest(),
                mime="text/plain",
                structured=d["structured"],
                parse_status="parsed",
                parse_method="replay",
            )
            session.add(doc)
            documents[dkey] = doc
    await session.flush()
    seqs: Counter[tuple[str, str]] = Counter()
    for r in records:
        dkey = (r["document"]["doc_type"], r["document"]["external_id"])
        ckey = (*dkey, tuple(r["labels"]))
        if ckey not in chunks:
            chunk = DocumentChunk(
                document_id=documents[dkey].id,
                seq=seqs[dkey],
                char_start=0,
                char_end=0,
                text="",
                labels=r["labels"],
            )
            session.add(chunk)
            chunks[ckey] = chunk
            seqs[dkey] += 1
    await session.flush()

    # The pipeline embeds the same text (process.py), so a deterministic embedder gives back the
    # vectors of the recorded run.
    vectors = await runtime.embedder.embed(
        [canonicalize(f"{r['title']} {r['summary']} {' '.join(r['keywords'])}") for r in records]
    )
    ids_by_key: dict[str, int] = {}
    for r, vec in zip(records, vectors, strict=True):
        dkey = (r["document"]["doc_type"], r["document"]["external_id"])
        signal = Signal(
            document_id=documents[dkey].id,
            chunk_id=chunks[(*dkey, tuple(r["labels"]))].id,
            observed_at=date.fromisoformat(r["observed_at"]),
            embedding=vec,
            dedupe_key=r["key"],
            **{name: r[name] for name in _SIGNAL_FIELDS},
        )
        session.add(signal)
        await session.flush()
        ids_by_key[r["key"]] = signal.id

    def run_order(rs: list[dict[str, Any]]) -> list[int]:
        # Documents by publication, and within one the signals as they were created. Records
        # are in creation order already; sorted() is stable.
        ordered = sorted(rs, key=lambda r: r["document"]["published_at"])
        return [ids_by_key[r["key"]] for r in ordered]

    runs = (
        [
            [r for r in records if r["document"]["doc_type"] in first],
            [r for r in records if r["document"]["doc_type"] not in first],
        ]
        if first
        else [records]
    )
    for run in runs:
        ids = run_order(run)
        for at in range(0, len(ids), LINK_SLICE):
            await link_signals(session, runtime, ids[at : at + LINK_SLICE], today=today)
            await session.flush()

    rows = (
        await session.execute(
            select(OpportunitySignal.opportunity_id, Signal.dedupe_key)
            .join(Signal, Signal.id == OpportunitySignal.signal_id)
            .where(Signal.id.in_(list(ids_by_key.values())))
        )
    ).all()
    by_opp: dict[int, list[str]] = defaultdict(list)
    for opp_id, key in rows:
        by_opp[opp_id].append(key)
    groups = sorted(sorted(keys) for keys in by_opp.values())
    result = ReplayResult(signals=len(records), linked=len(rows), groups=groups)
    recorded = recorded_groups(records)
    if recorded:
        mine = {tuple(g) for g in groups}
        result.recorded = len(recorded)
        result.same_as_recorded = sum(1 for g in recorded if tuple(g) in mine)
    return result
