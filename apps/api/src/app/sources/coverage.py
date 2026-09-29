"""Read-only inventory of configured scope, collected records, and retained evidence.

Provider-wide API access is not evidence that every institution was collected. Publication
dates without recorded provenance remain unspecified. Local files are checked for readability;
remote object references stay unverified and this report never calls a provider or cloud API.
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from collections.abc import AsyncIterable, AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, IngestRun, InstitutionRow, Source
from app.domain.institutions import compact_name

RawStatus = Literal["available", "missing", "unverified"]


@dataclass(frozen=True, slots=True)
class CoverageDocument:
    institution_code: str | None
    source_id: int
    raw_uri: str | None
    published_at: date | None
    published_from: str | None
    council_id: str | None
    synthetic: bool = False
    has_inline_raw: bool = False


def coverage_documents_query() -> Select[Any]:
    """Narrow streaming projection; no text, embeddings, or full structured payloads."""
    return (
        select(
            Document.institution_code,
            Document.source_id,
            Document.raw_uri,
            Document.published_at,
            Document.structured["published_from"].astext,
            Document.structured["council_id"].astext,
            and_(
                func.jsonb_typeof(Document.structured["raw"]) == "object",
                Document.structured["raw"].astext != "{}",
            ).label("has_inline_raw"),
        )
        .join(Source, Source.id == Document.source_id)
        .where(
            ~Source.key.startswith("fixture_", autoescape=True),
            Source.adapter != "fixture",
            Document.structured["synthetic"].astext.is_distinct_from("true"),
        )
    )


def latest_source_runs_query() -> Select[IngestRun]:
    """Latest run per source; the source/time index supports this read-only diagnostic."""
    return (
        select(IngestRun)
        .distinct(IngestRun.source_id)
        .order_by(IngestRun.source_id, IngestRun.started_at.desc(), IngestRun.id.desc())
    )


def _counts() -> dict[str, Any]:
    return {
        "collected": 0,
        "raw_referenced": 0,
        "raw_available": 0,
        "raw_missing": 0,
        "raw_unverified": 0,
        "raw_inline": 0,
        "publication_provenance": Counter(),
        "first_published_at": None,
        "last_published_at": None,
        "source_keys": set(),
    }


def _record(counts: dict[str, Any], document: CoverageDocument, key: str, raw: RawStatus) -> None:
    counts["collected"] += 1
    counts["raw_referenced"] += bool(document.raw_uri or document.has_inline_raw)
    counts[f"raw_{raw}"] += 1
    counts["raw_inline"] += document.has_inline_raw
    counts["publication_provenance"][document.published_from or "unspecified"] += 1
    counts["source_keys"].add(key)
    if document.published_at is not None:
        day = document.published_at.isoformat()
        counts["first_published_at"] = min(counts["first_published_at"] or day, day)
        counts["last_published_at"] = max(counts["last_published_at"] or day, day)


def _serializable(counts: dict[str, Any]) -> dict[str, Any]:
    return counts | {
        "publication_provenance": dict(sorted(counts["publication_provenance"].items())),
        "source_keys": sorted(counts["source_keys"]),
    }


def _local_raw_status(uri: str) -> RawStatus:
    if not uri.startswith("file://"):
        return "unverified"
    try:
        with Path(uri.removeprefix("file://")).open("rb") as handle:
            return "available" if handle.read(1) else "missing"
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unverified"


def _targets(
    source: Source,
    institutions: Sequence[InstitutionRow],
    provider_ids: dict[tuple[int, str], set[str]],
) -> tuple[str, set[str], list[str]]:
    """Resolve only implemented adapter filters; unknown/unbounded scope stays explicit."""
    codes = {institution.code for institution in institutions}
    config = source.config or {}
    resolved: set[str] = set()
    unmapped: set[str] = set()
    if source.adapter == "crawler":
        for board in config.get("boards") or []:
            if not isinstance(board, dict):
                continue
            code = str(board.get("institution_code") or "")
            url = urlsplit(str(board.get("url") or ""))
            if code in codes and url.scheme in {"https", "http"} and url.hostname:
                resolved.add(code)
            else:
                unmapped.add(code or "board_without_institution_code")
        return "explicit_boards", resolved, sorted(unmapped)
    if source.adapter == "clik":
        targets = config.get("council_ids") or []
        for target in targets:
            candidates = provider_ids.get((source.id, str(target)), set()) & codes
            if len(candidates) == 1:
                resolved.update(candidates)
            else:
                unmapped.add(str(target))
        return "explicit_council_ids" if targets else "unbounded", resolved, sorted(unmapped)
    if source.adapter == "lofin":
        targets = config.get("institutions") or []
        names: dict[str, set[str]] = defaultdict(set)
        for institution in institutions:
            for name in [institution.name, *(institution.aliases or [])]:
                names[compact_name(name)].add(institution.code)
        for target in targets:
            candidates = names.get(compact_name(str(target)), set())
            if len(candidates) == 1:
                resolved.update(candidates)
            else:
                unmapped.add(str(target))
        return "explicit_institution_names" if targets else "unbounded", resolved, sorted(unmapped)
    return "unbounded" if source.adapter == "g2b" else "unsupported", resolved, []


async def build_coverage_report(
    institutions: Sequence[InstitutionRow],
    sources: Sequence[Source],
    documents: AsyncIterable[CoverageDocument],
    latest_runs: Sequence[IngestRun],
    *,
    verify_raw: bool = True,
) -> dict[str, Any]:
    real_sources = {
        source.id: source
        for source in sources
        if not source.key.startswith("fixture_") and source.adapter != "fixture"
    }
    counts = {institution.code: _counts() for institution in institutions}
    per_source = {source_id: _counts() for source_id in real_sources}
    total, unresolved = _counts(), _counts()
    provider_ids: dict[tuple[int, str], set[str]] = defaultdict(set)
    raw_cache: dict[str, RawStatus] = {}
    async for document in documents:
        source = real_sources.get(document.source_id)
        if source is None or document.synthetic:
            continue
        if document.has_inline_raw:
            raw: RawStatus = "available"
        elif not document.raw_uri:
            raw = "missing"
        elif not verify_raw:
            raw = "unverified"
        else:
            if document.raw_uri not in raw_cache:
                raw_cache[document.raw_uri] = await asyncio.to_thread(
                    _local_raw_status, document.raw_uri
                )
            raw = raw_cache[document.raw_uri]
        institution_counts = counts.get(document.institution_code or "", unresolved)
        for target in (institution_counts, total, per_source[source.id]):
            _record(target, document, source.key, raw)
        if document.institution_code in counts and document.council_id:
            assert document.institution_code is not None
            provider_ids[source.id, document.council_id].add(document.institution_code)

    configured: dict[str, set[str]] = defaultdict(set)
    enabled: dict[str, set[str]] = defaultdict(set)
    source_rows: list[dict[str, Any]] = []
    runs = {run.source_id: run for run in latest_runs}
    for source in sorted(real_sources.values(), key=lambda value: value.key):
        scope, targets, unmapped = _targets(source, institutions, provider_ids)
        for code in targets:
            configured[code].add(source.key)
            if source.enabled:
                enabled[code].add(source.key)
        run = runs.get(source.id)
        source_rows.append(
            {
                "key": source.key,
                "adapter": source.adapter,
                "enabled": bool(source.enabled),
                "scope": scope,
                "configured_institution_codes": sorted(targets),
                "unmapped_targets": unmapped,
                "last_run_at": source.last_run_at.isoformat() if source.last_run_at else None,
                "last_success_at": source.last_success_at.isoformat()
                if source.last_success_at
                else None,
                "consecutive_failures": source.consecutive_failures or 0,
                "latest_status": run.status if run else "never_recorded",
                **_serializable(per_source[source.id]),
            }
        )
    institution_rows = [
        {
            "code": institution.code,
            "name": institution.name,
            "kind": institution.kind,
            "configured_sources": len(configured[institution.code]),
            "enabled_configured_sources": len(enabled[institution.code]),
            "configured_source_keys": sorted(configured[institution.code]),
            **_serializable(counts[institution.code]),
        }
        for institution in sorted(institutions, key=lambda value: value.code)
    ]
    return {
        "scope": "stored_evidence_inventory",
        "raw_verification": "local_readability_and_inline_json"
        if verify_raw
        else "inline_json_only",
        "limitations": [
            "Reference/configuration counts do not establish national or complete source coverage.",
            "Remote raw object references are unverified; no external requests were made.",
            "Raw availability checks readability or inline JSON presence, not content hash integrity or fidelity to the original publication.",
            "Publication dates retain recorded provenance; unspecified dates are not verified publication dates.",
            "CLIK council IDs are mapped only from stored documents, not an assumed national crosswalk.",
        ],
        "summary": {
            "reference_institutions": len(institutions),
            "institutions_configured": sum(bool(configured[row.code]) for row in institutions),
            "institutions_collected": sum(
                bool(counts[row.code]["collected"]) for row in institutions
            ),
            "institutions_raw_available": sum(
                bool(counts[row.code]["raw_available"]) for row in institutions
            ),
            **_serializable(total),
        },
        "institutions": institution_rows,
        "sources": source_rows,
        "unresolved": _serializable(unresolved),
    }


async def coverage_report(session: AsyncSession, *, verify_raw: bool = True) -> dict[str, Any]:
    institutions = list((await session.scalars(select(InstitutionRow))).all())
    sources = list((await session.scalars(select(Source))).all())
    latest_runs = list((await session.scalars(latest_source_runs_query())).all())
    stream = await session.stream(coverage_documents_query().execution_options(yield_per=1000))

    async def documents() -> AsyncIterator[CoverageDocument]:
        async for code, source_id, raw, published, provenance, council_id, inline in stream:
            yield CoverageDocument(
                code, source_id, raw, published, provenance, council_id, has_inline_raw=bool(inline)
            )

    try:
        return await build_coverage_report(
            institutions, sources, documents(), latest_runs, verify_raw=verify_raw
        )
    finally:
        await stream.close()
