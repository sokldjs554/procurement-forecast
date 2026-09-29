"""Exercise the actual export query and frozen JSONL without modifying review history."""

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Document,
    DocumentChunk,
    InstitutionRow,
    Organization,
    ReviewItem,
    Signal,
    Source,
    User,
)
from app.db.session import get_sessionmaker
from app.eval.review_dataset import export_review_dataset

TEXT = (
    "○위원 김민수  스마트쉘터를 설치해 주십시오.\n○교통과장 이민수  스마트쉘터를 설치할 계획입니다."
)
POSITIVE = "스마트쉘터를 설치할 계획입니다."
NEGATIVE = "스마트쉘터를 설치해 주십시오."
RESOLVED = datetime(2026, 9, 28, 12, tzinfo=UTC)
SOURCE = "test_review_dataset_export"


async def seed_reviews(session: AsyncSession) -> tuple[list[Signal], list[ReviewItem]]:
    institution = InstitutionRow(
        code="LG-REVIEW-DATASET",
        name="평가시",
        kind="local_gov",
        sido="경기도",
        region_code="41130",
    )
    org = Organization(name="Review exporter")
    source = Source(
        key=SOURCE,
        name="review source",
        adapter="fixture",
        enabled=False,
        config={"password": "SOURCE_SECRET"},
    )
    session.add_all([institution, org, source])
    await session.flush()
    user = User(
        org_id=org.id,
        email="private-reviewer@example.test",
        name="Private Name",
        password_hash="PRIVATE_PASSWORD",
        role="owner",
    )
    session.add(user)
    await session.flush()
    docs = []
    chunks = []
    for index in range(2):
        document = Document(
            source_id=source.id,
            external_id=f"export-{index}",
            doc_type="council_minutes",
            title="평가용 회의록",
            published_at=date(2026, 9, 27),
            mime="text/plain",
            content_hash=hashlib.sha256(TEXT.encode()).hexdigest(),
            parse_status="parsed",
            text=TEXT if index == 0 else None,
            raw_uri="s3://private?token=RAW_SECRET",
            structured={},
        )
        session.add(document)
        await session.flush()
        docs.append(document)
        chunk = DocumentChunk(
            document_id=document.id, seq=0, char_start=0, char_end=len(TEXT), text=TEXT
        )
        session.add(chunk)
        chunks.append(chunk)
    await session.flush()
    signals, reviews = [], []
    for index, status in enumerate(
        ("approved", "edited", "rejected", "approved", "open", "approved")
    ):
        doc_index = 1 if index == 3 else 0
        quote = NEGATIVE if status == "rejected" else POSITIVE
        start = TEXT.index(quote)
        signal = Signal(
            document_id=docs[doc_index].id,
            chunk_id=chunks[doc_index].id,
            stage="council_mention",
            institution_code=institution.code,
            title="스마트쉘터 설치",
            summary="설치 계획",
            category="mobility",
            keywords=["스마트쉘터"],
            commitment="planned",
            confidence=0.95,
            evidence=[{"quote": quote, "start": start, "end": start + len(quote)}],
            grounding={},
            verdict="rejected" if status == "rejected" else "accepted",
            extractor="test-v1",
            observed_at=date(2026, 9, 27),
            dedupe_key=f"review-dataset-{index}",
        )
        session.add(signal)
        await session.flush()
        changes = (
            {"category": {"from": "smart_city", "to": "mobility"}} if status == "edited" else {}
        )
        review = ReviewItem(
            signal_id=signal.id,
            status=status,
            reasons=["private@example.test"],
            resolution={
                "action": "reject" if status == "rejected" else "approve",
                "changes": changes,
                "note": "PRIVATE_NOTE",
            },
            resolved_by=user.id,
            resolved_at=datetime(2026, 10, 1, tzinfo=UTC) if index == 5 else RESOLVED,
        )
        session.add(review)
        signals.append(signal)
        reviews.append(review)
    await session.flush()
    return signals, reviews


async def test_sql_export_repeatability_exclusions_and_history(migrated_db, tmp_path: Path):  # type: ignore[no-untyped-def]
    async with get_sessionmaker()() as session:
        signals, reviews = await seed_reviews(session)
        ids = [signal.id for signal in signals]
        original = [(r.status, json.dumps(r.resolution, sort_keys=True)) for r in reviews]
        one, two = tmp_path / "one.jsonl", tmp_path / "two.jsonl"
        report = await export_review_dataset(session, one, source_keys=[SOURCE])
        again = await export_review_dataset(session, two, source_keys=[SOURCE])
        assert one.read_bytes() == two.read_bytes() and report.digest == again.digest
        assert report.candidate_reviews == 5 and report.exported == 4 and report.excluded == 1
        assert report.excluded_reasons["source_text_missing"] == 1
        assert not report.has_more and not report.truncated
        lines = [json.loads(line) for line in one.read_text().splitlines()]
        assert lines[0]["record_type"] == "review_dataset_manifest"
        assert len({row["split"] for row in lines[1:]}) == 1
        rejected = next(row for row in lines[1:] if row["human_label"]["status"] == "rejected")
        assert rejected["human_label"]["decision"] == "reject"
        edited = next(row for row in lines[1:] if row["human_label"]["status"] == "edited")
        assert edited["human_label"]["reviewed_fields"] == ["category"]
        for forbidden in ("PRIVATE_", "RAW_SECRET", "SOURCE_SECRET", "@example.test"):
            assert forbidden not in one.read_text()
        assert not session.new and not session.dirty and not session.deleted
        assert [(r.status, json.dumps(r.resolution, sort_keys=True)) for r in reviews] == original
        assert (
            await session.scalar(select(func.count()).select_from(Signal).where(Signal.id.in_(ids)))
            == 6
        )
        await session.rollback()


async def test_sql_export_limit_and_cutoff_are_explicit(migrated_db, tmp_path: Path):  # type: ignore[no-untyped-def]
    async with get_sessionmaker()() as session:
        await seed_reviews(session)
        partial = await export_review_dataset(
            session, tmp_path / "partial.jsonl", source_keys=[SOURCE], limit=2
        )
        assert partial.exported == 2 and partial.has_more and partial.truncated
        cutoff = datetime(2026, 9, 29, tzinfo=UTC)
        report = await export_review_dataset(
            session, tmp_path / "cutoff.jsonl", source_keys=[SOURCE], resolved_before=cutoff
        )
        assert report.candidate_reviews == 4 and report.exported == 3
        manifest = json.loads((tmp_path / "cutoff.jsonl").read_text().splitlines()[0])
        assert manifest["scope"]["resolved_before"] == "2026-09-29T00:00:00+00:00"
        assert manifest["temporal_scope"] == "current_stored_snapshot"
        await session.rollback()
