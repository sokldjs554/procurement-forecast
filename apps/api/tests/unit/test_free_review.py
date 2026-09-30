"""Review persistence checks must cover JSON evidence without accepting missing PDF bytes."""

import importlib.util
from pathlib import Path

import pytest

from app.db.models import Document

_path = Path(__file__).resolve().parents[4] / "scripts" / "free-review-data.py"
_spec = importlib.util.spec_from_file_location("free_review_data", _path)
assert _spec is not None and _spec.loader is not None
review = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review)


async def test_review_fingerprints_structured_evidence_without_a_file() -> None:
    doc = Document(
        id=1,
        mime="application/json",
        title="공고",
        content_hash="abc",
        structured={"amount_krw": 1000},
        raw_uri=None,
    )
    before = await review.document_fingerprint(doc)
    doc.structured = {"amount_krw": 2000}
    assert await review.document_fingerprint(doc) != before


async def test_review_still_rejects_a_missing_pdf_original(tmp_path: Path) -> None:
    doc = Document(
        id=2,
        mime="application/pdf",
        title="예산서",
        content_hash="abc",
        structured={"pages": 1},
        raw_uri=None,
    )
    with pytest.raises(RuntimeError, match="raw evidence"):
        await review.document_fingerprint(doc)
    doc.raw_uri = (tmp_path / "lost.pdf").as_uri()
    with pytest.raises(FileNotFoundError):
        await review.document_fingerprint(doc)


async def test_review_detects_changed_original_bytes(tmp_path: Path) -> None:
    raw = tmp_path / "original.pdf"
    raw.write_bytes(b"original bytes")
    doc = Document(
        id=3,
        mime="application/pdf",
        title="예산서",
        content_hash="abc",
        structured={},
        raw_uri=raw.as_uri(),
    )
    before = await review.document_fingerprint(doc)
    raw.write_bytes(b"changed bytes")
    assert await review.document_fingerprint(doc) != before
