"""Collect: adapter → ``documents`` (idempotent upsert keyed by (source, external_id)).

A document is re-processed only when its content hash changes (e.g. a 정정공고 edits a notice),
so hourly syncs over overlapping windows are cheap and safe.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, IngestRun, InstitutionRow, Source
from app.domain.institutions import (
    PROVIDER_CODE_PREFIX,
    Institution,
    Resolution,
    compact_name,
    looks_like_local_government,
    names_no_institution,
    provider_institution,
)
from app.log import get_logger
from app.runtime import Runtime
from app.sources.base import FetchWindow, RawRecord, SkipsStored, SourceAdapter
from app.storage import store_raw

log = get_logger(__name__)


@dataclass(slots=True)
class IngestStats:
    fetched: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    changed_ids: list[int] = field(default_factory=list)


def default_window(source: Source, today: date, *, backfill_days: int = 30) -> FetchWindow:
    """Resume from the cursor with a small overlap (providers publish late)."""
    last = source.cursor.get("until")
    if last:
        since = date.fromisoformat(last) - timedelta(days=3)
    else:
        since = today - timedelta(days=backfill_days)
    return FetchWindow(since=since, until=today)


async def resolve_institution(
    session: AsyncSession,
    runtime: Runtime,
    raw: str | None,
    *,
    code_hint: str | None = None,
    sido_hint: str | None = None,
    provider_code: str | None = None,
) -> Resolution:
    """Resolve by our table first. A record the table cannot place but whose provider vouches
    for it with a code (조달청 수요기관코드) becomes an institution of its own, stored so the API
    can name it. Names shaped like a 지자체 are the exception: one the table misses is a gap in
    the table, so it goes to review instead of becoming a second copy of that 지자체."""
    if not provider_code and raw and raw.strip() and not runtime.registry.knows_name(raw):
        await _load_provider_institution_named(session, runtime, raw)
    resolution = runtime.registry.resolve(
        raw, code_hint=code_hint, sido_hint=sido_hint, provider_code=provider_code
    )
    if (
        resolution.method != "none"
        or not provider_code
        or not raw
        or not raw.strip()
        or looks_like_local_government(raw)
        or names_no_institution(raw)
    ):
        return resolution
    inst = await _store_provider_institution(session, provider_institution(provider_code, raw))
    runtime.registry.add(inst)
    return Resolution(inst, None, 1.0, "provider")


async def _load_provider_institution_named(
    session: AsyncSession, runtime: Runtime, raw: str
) -> None:
    """A record without a code (every 사전규격) finds a coded institution by its exact name. The
    registry only holds what this process stored itself, so look in the table too: after a
    restart, in another worker, or in `pipeline reresolve`, the in-memory registry starts from
    the CSV and missed 558 사전규격 of 30 days of live data (2026-09-26)."""
    row = await session.scalar(
        select(InstitutionRow)
        .where(
            InstitutionRow.code.startswith(PROVIDER_CODE_PREFIX),
            # Stored names are normalize()d, so spaces are the only whitespace left.
            func.replace(InstitutionRow.name, " ", "") == compact_name(raw),
        )
        .order_by(InstitutionRow.code)
        .limit(1)
    )
    if row is not None:
        runtime.registry.add(_institution_from_row(row))


def _institution_from_row(row: InstitutionRow) -> Institution:
    return Institution(
        code=row.code,
        name=row.name,
        kind=row.kind,  # type: ignore[arg-type]
        sido=row.sido,
        sigungu=None,
        region_code=row.region_code,
    )


async def _store_provider_institution(session: AsyncSession, inst: Institution) -> Institution:
    await session.execute(
        insert(InstitutionRow)
        .values(
            code=inst.code,
            name=inst.name,
            kind=inst.kind,
            sido=inst.sido,
            sigungu=None,
            region_code=inst.region_code,
            executive_code=None,
            aliases=[],
        )
        .on_conflict_do_nothing(index_elements=["code"])
    )
    # Another worker, or this one before a restart, may have stored it first: that row wins.
    row = await session.get(InstitutionRow, inst.code)
    assert row is not None
    return _institution_from_row(row)


async def reresolve_institutions(session: AsyncSession, runtime: Runtime) -> dict[str, object]:
    """Resolve again every document that had no institution at ingest, with the current table,
    and queue the ones that now resolve for processing. Stored documents keep the provider's
    code in ``structured``, so a better table needs no refetch (and no API quota)."""
    from app.pipeline.process import ReprocessingProtectedError, protect_human_decisions

    code = Document.structured["provider_institution_code"].astext
    rows = (
        await session.execute(
            select(Document.id, Document.publisher_raw, code)
            .where(Document.institution_code.is_(None))
            .order_by(Document.id)
            .with_for_update()
        )
    ).all()
    groups: dict[tuple[str | None, str | None], list[int]] = {}
    for doc_id, raw, provider_code in rows:
        groups.setdefault((raw, provider_code), []).append(doc_id)
    by_method: Counter[str] = Counter()
    protected = 0
    before = len(runtime.registry)
    # Coded names first: 사전규격 records carry no 수요기관코드 at all (0 of 9,286 on live data,
    # 2026-09-26), so they find a school or 공단 only by the exact name a coded 발주계획 or
    # 입찰공고 registered. Row order would leave that to chance.
    for (raw, provider_code), ids in sorted(groups.items(), key=lambda kv: kv[0][1] is None):
        res = await resolve_institution(session, runtime, raw, provider_code=provider_code)
        if res.institution is None:
            by_method[res.method] += len(ids)
            continue
        writable_ids: list[int] = []
        for document_id in ids:
            try:
                await protect_human_decisions(session, document_id)
            except ReprocessingProtectedError:
                protected += 1
            else:
                writable_ids.append(document_id)
        by_method[res.method] += len(writable_ids)
        if not writable_ids:
            continue
        await session.execute(
            update(Document)
            .where(Document.id.in_(writable_ids))
            .values(
                institution_code=res.institution.code,
                department=func.coalesce(res.department, Document.department),
                structured=Document.structured.op("||")(
                    func.jsonb_build_object("institution_resolution", res.method)
                ),
                parse_status="pending",
            )
        )
    await session.flush()
    resolved = sum(n for m, n in by_method.items() if m not in ("none", "ambiguous"))
    return {
        "documents": len(rows),
        "names": len(groups),
        "resolved": resolved,
        "protected": protected,
        "by_method": dict(by_method.most_common()),
        "institutions_added": len(runtime.registry) - before,
    }


async def upsert_record(
    session: AsyncSession, source: Source, rec: RawRecord, runtime: Runtime
) -> tuple[Document, str]:
    """Returns (document, "created" | "updated" | "skipped")."""
    from app.pipeline.process import protect_human_decisions

    content_hash = rec.content_hash()
    existing = await session.scalar(
        select(Document)
        .where(Document.source_id == source.id, Document.external_id == rec.external_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if existing is not None and existing.content_hash == content_hash:
        return existing, "skipped"
    if existing is not None:
        # Source text and raw bytes are evidence for persisted human decisions. Refuse
        # replacement before raw storage or metadata changes, under Document-first locks.
        await protect_human_decisions(session, existing.id)

    resolution = await resolve_institution(
        session,
        runtime,
        rec.publisher_raw,
        code_hint=rec.institution_code_hint,
        sido_hint=rec.sido_hint,
        provider_code=rec.provider_institution_code,
    )
    raw_uri = await store_raw(source.key, rec.external_id, rec.content) if rec.content else None
    fields = {
        "doc_type": rec.doc_type,
        "title": rec.title,
        "url": rec.url,
        "publisher_raw": rec.publisher_raw,
        "institution_code": resolution.institution.code if resolution.institution else None,
        "department": resolution.department or rec.structured.get("department"),
        "published_at": rec.published_at,
        "content_hash": content_hash,
        "mime": rec.mime,
        "raw_uri": raw_uri,
        "structured": rec.structured | {"institution_resolution": resolution.method},
        "parse_status": "pending",
        "parse_error": None,
    }
    if existing is None:
        doc = Document(source_id=source.id, external_id=rec.external_id, **fields)
        session.add(doc)
        await session.flush()
        return doc, "created"
    for key, value in fields.items():
        setattr(existing, key, value)
    existing.text = None
    await session.flush()
    return existing, "updated"


async def run_ingest(
    session: AsyncSession,
    source: Source,
    adapter: SourceAdapter,
    runtime: Runtime,
    window: FetchWindow,
) -> IngestStats:
    run = IngestRun(source_id=source.id, status="running")
    session.add(run)
    await session.flush()
    stats = IngestStats()
    if isinstance(adapter, SkipsStored):

        async def known(ids: list[str]) -> set[str]:
            stored = await session.scalars(
                select(Document.external_id).where(
                    Document.source_id == source.id, Document.external_id.in_(ids)
                )
            )
            return set(stored.all())

        adapter.known_external_ids = known
    try:
        async for rec in adapter.fetch(window):
            stats.fetched += 1
            doc, outcome = await upsert_record(session, source, rec, runtime)
            if outcome == "skipped":
                stats.skipped += 1
            else:
                setattr(stats, outcome, getattr(stats, outcome) + 1)
                stats.changed_ids.append(doc.id)
        run.status = "succeeded"
        source.cursor = {"until": window.until.isoformat()}
        source.last_success_at = datetime.now(UTC)
        source.consecutive_failures = 0
    except Exception as exc:
        run.status = "partial" if stats.fetched else "failed"
        run.error = f"{type(exc).__name__}: {exc}"[:2000]
        source.consecutive_failures += 1
        raise
    finally:
        run.finished_at = datetime.now(UTC)
        run.fetched, run.created, run.updated, run.skipped = (
            stats.fetched,
            stats.created,
            stats.updated,
            stats.skipped,
        )
        run.stats = {"window": [window.since.isoformat(), window.until.isoformat()]}
        source.last_run_at = datetime.now(UTC)
        await adapter.aclose()
    log.info(
        "ingest.done",
        source=source.key,
        **{k: getattr(stats, k) for k in ("fetched", "created", "updated", "skipped")},
    )
    return stats
