"""The revision lookup must be wired through real database ingestion and review locks."""

from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.db.models import Document, ReviewItem, Signal, Source
from app.db.session import get_sessionmaker
from app.pipeline.ingest import default_window, run_ingest
from app.pipeline.process import ReprocessingProtectedError
from app.runtime import Runtime
from app.sources.base import FetchWindow
from app.sources.clik import ClikMinutesAdapter
from app.sources.coverage import coverage_report
from app.sources.http import ResilientClient
from app.sources.resilience import MemoryBreaker, MemoryLimiter
from app.storage import load_raw

WINDOW = FetchWindow(date(2026, 9, 1), date(2026, 9, 28))


def _adapter(key: str, docids: list[str]) -> ClikMinutesAdapter:
    def row(docid: str) -> dict[str, str]:
        return {
            "DOCID": docid,
            "RASMBLY_ID": "031013",
            "RASMBLY_NM": "경기도 성남시의회",
            "RASMBLY_NUMPR": "10",
            "RASMBLY_SESN": "312",
            "MINTS_ODR": "2",
            "MTG_DE": "20260907",
            "MTGNM": "본회의",
        }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["displayType"] == "list":
            payload: dict[str, Any] = {"LIST": [{"ROW": row(docid)} for docid in docids]}
        else:
            docid = request.url.params["docid"]
            payload = row(docid) | {"MINTS_HTML": f"<p>{docid}: 실제 수정된 회의록 근거</p>"}
        return httpx.Response(200, json=[{"RESULT_CODE": "SUCCESS", **payload}])

    client = ResilientClient(
        key,
        base_url="https://clik.test",
        limiter=MemoryLimiter(),
        breaker=MemoryBreaker(),
        transport=httpx.MockTransport(handler),
        max_attempts=1,
    )
    return ClikMinutesAdapter(client, "test-key", key=key)


async def test_ingest_wires_revision_metadata_and_updates_only_a_new_docid(
    demo_world: Any,
    runtime: Runtime,
) -> None:
    async with get_sessionmaker()() as session:
        source = Source(key="test_clik_revisions", name="revisions", adapter="clik", enabled=False)
        session.add(source)
        await session.flush()
        first = await run_ingest(session, source, _adapter(source.key, ["OLD"]), runtime, WINDOW)
        assert first.created == 1
        doc = await session.get(Document, first.changed_ids[0])
        assert doc is not None and doc.structured["docid"] == "OLD"
        original_id = doc.id
        assert source.cursor["until"] == "2026-09-28"
        incremental = default_window(source, date(2026, 9, 29))
        assert incremental.since > date(2026, 9, 7)

        revised = await run_ingest(
            session, source, _adapter(source.key, ["OLD", "NEW"]), runtime, incremental
        )
        assert (revised.created, revised.updated) == (0, 1)
        assert revised.changed_ids == [original_id]
        assert doc.structured["docid"] == "NEW"
        assert (
            doc.raw_uri and await load_raw(doc.raw_uri) == "NEW: 실제 수정된 회의록 근거".encode()
        )

        unchanged_adapter = _adapter(source.key, ["NEW", "OLD"])
        unchanged = await run_ingest(
            session,
            source,
            unchanged_adapter,
            runtime,
            default_window(source, date(2026, 9, 30)),
        )
        assert unchanged.fetched == 0
        assert unchanged_adapter.stats["details"] == 0
        assert doc.structured["docid"] == "NEW"
        await session.rollback()


async def test_new_clik_revision_cannot_overwrite_human_reviewed_evidence(
    demo_world: Any,
    runtime: Runtime,
) -> None:
    async with get_sessionmaker()() as session:
        source = Source(
            key="test_clik_protected_revision", name="protected", adapter="clik", enabled=False
        )
        session.add(source)
        await session.flush()
        first = await run_ingest(session, source, _adapter(source.key, ["OLD"]), runtime, WINDOW)
        doc = await session.get(Document, first.changed_ids[0])
        assert doc is not None and doc.raw_uri
        signal = Signal(
            document_id=doc.id,
            stage="council_mention",
            title="검토된 근거",
            category="other",
            extractor="test",
            observed_at=date(2026, 9, 7),
            dedupe_key="test_clik_protected_revision",
        )
        session.add(signal)
        await session.flush()
        session.add(ReviewItem(signal_id=signal.id, status="approved", reasons=["human_review"]))
        await session.flush()
        original = (doc.content_hash, doc.raw_uri, dict(doc.structured), doc.url)
        original_bytes = await load_raw(doc.raw_uri)

        with pytest.raises(ReprocessingProtectedError, match="human decisions"):
            await run_ingest(session, source, _adapter(source.key, ["OLD", "NEW"]), runtime, WINDOW)
        assert (doc.content_hash, doc.raw_uri, doc.structured, doc.url) == original
        assert await load_raw(doc.raw_uri) == original_bytes
        assert (
            await session.scalar(select(ReviewItem.status).where(ReviewItem.signal_id == signal.id))
            == "approved"
        )
        await session.rollback()


async def test_coverage_database_projection_preserves_inline_raw_and_publication_provenance(
    demo_world: Any,
    tmp_path: Path,
) -> None:
    async with get_sessionmaker()() as session:
        source = Source(
            key="test_coverage_sql",
            name="coverage",
            adapter="crawler",
            enabled=True,
            config={"boards": [{"institution_code": "CN-41130", "url": "https://council.test"}]},
        )
        session.add(source)
        await session.flush()
        raw = tmp_path / "raw.txt"
        raw.write_text("저장 원문")
        session.add_all(
            [
                Document(
                    source_id=source.id,
                    external_id="inline",
                    doc_type="bid_notice",
                    title="JSON",
                    institution_code="CN-41130",
                    published_at=date(2026, 9, 1),
                    content_hash="a" * 64,
                    mime="application/json",
                    structured={"raw": {"record": "kept"}},
                ),
                Document(
                    source_id=source.id,
                    external_id="file",
                    doc_type="council_minutes",
                    title="text",
                    institution_code="CN-41130",
                    published_at=date(2026, 9, 2),
                    content_hash="b" * 64,
                    mime="text/plain",
                    raw_uri=raw.as_uri(),
                    structured={"published_from": "meeting_date"},
                ),
            ]
        )
        await session.flush()
        report = await coverage_report(session)
        row = next(item for item in report["sources"] if item["key"] == source.key)
        assert (row["collected"], row["raw_available"], row["raw_inline"]) == (2, 2, 1)
        assert row["publication_provenance"] == {"unspecified": 1, "meeting_date": 1}
        assert row["configured_institution_codes"] == ["CN-41130"]
        await session.rollback()
