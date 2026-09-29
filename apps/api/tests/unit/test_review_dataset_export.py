"""Human judgements need source provenance and explicit limits before evaluation reuse."""

import hashlib
import json
from datetime import UTC, date, datetime

import pytest

from app.db.models import Document, DocumentChunk, ReviewItem, Signal

TEXT = "○교통과장 이민수  스마트쉘터를 설치할 계획입니다."
QUOTE = "스마트쉘터를 설치할 계획입니다."
RESOLVED = datetime(2026, 9, 28, 12, tzinfo=UTC)


def reviewed_case(
    *,
    review_id: int = 1,
    document_id: int = 1,
    status: str = "approved",
    text: str = TEXT,
    quote: str = QUOTE,
    content_hash: str | None = None,
):  # type: ignore[no-untyped-def]
    from app.eval.review_dataset import ReviewedSignal

    document = Document(
        id=document_id,
        source_id=1,
        doc_type="council_minutes",
        title="회의록",
        content_hash=content_hash or hashlib.sha256(text.encode()).hexdigest(),
        text=text,
        published_at=date(2026, 9, 27),
        parse_status="parsed",
        structured={},
        raw_uri="s3://private?secret=DO_NOT_EXPORT",
        url="https://source.test/?token=PRIVATE_TOKEN",
    )
    start = text.index(quote)
    signal = Signal(
        id=review_id,
        document_id=document_id,
        chunk_id=document_id,
        stage="council_mention",
        institution_code="LG-41130",
        title="스마트쉘터 설치",
        summary="설치 계획",
        category="smart_city",
        keywords=["스마트쉘터"],
        budget_krw=None,
        expected_year=None,
        expected_half=None,
        commitment="planned",
        procurement_type="goods",
        confidence=0.95,
        observed_at=date(2026, 9, 27),
        evidence=[{"quote": quote, "start": start, "end": start + len(quote)}],
        grounding={},
        verdict="rejected" if status == "rejected" else "accepted",
        extractor="test-v1",
    )
    review = ReviewItem(
        id=review_id,
        signal_id=review_id,
        status=status,
        reasons=["do-not-export@example.test"],
        resolved_by=7,
        resolved_at=RESOLVED,
        resolution={
            "action": "reject" if status == "rejected" else "approve",
            "changes": {},
            "note": "SECRET_NOTE",
            "email": "private@example.test",
        },
    )
    chunk = DocumentChunk(
        id=document_id, document_id=document_id, char_start=0, char_end=len(text), text=text
    )
    return ReviewedSignal(signal=signal, document=document, review=review, chunk=chunk)


def test_export_is_a_frozen_human_label_not_all_fields_gold() -> None:
    from app.eval.review_dataset import assemble_review_dataset

    case = reviewed_case()
    dataset = assemble_review_dataset([case])
    row = dataset.records[0]
    assert row["record_type"] == "human_review_sample"
    assert row["source"]["text"] == TEXT
    assert row["source"]["text_sha256"] == hashlib.sha256(TEXT.encode()).hexdigest()
    assert row["evidence"][0]["start"] == 11
    assert row["evidence"][0]["source_quote"] == QUOTE
    assert row["human_label"]["status"] == "approved"
    assert row["human_label"]["origin"] == "human_review"
    assert row["human_label"]["reviewed_fields"] == []
    assert row["human_label"]["gold_standard"] is False
    assert row["human_label"]["actor_user_id"] == 7
    assert row["human_label"]["resolved_at"] == "2026-09-28T12:00:00+00:00"
    assert case.signal.verdict == "accepted" and case.review.status == "approved"
    exported = dataset.jsonl_bytes().decode()
    assert "SECRET_NOTE" not in exported and "PRIVATE_TOKEN" not in exported
    assert "DO_NOT_EXPORT" not in exported and "@example.test" not in exported


def test_actual_edit_action_is_exported_with_its_category_correction() -> None:
    from app.eval.review_dataset import assemble_review_dataset

    case = reviewed_case(status="edited")
    case.review.resolution = {
        "action": "edit",
        "changes": {"category": {"from": "other", "to": "smart_city"}},
        "history": [{"action": "edit", "resolved_at": RESOLVED.isoformat()}],
    }
    dataset = assemble_review_dataset([case])
    assert dataset.report.exported == 1
    assert dataset.records[0]["human_label"]["reviewed_fields"] == ["category"]


def test_human_review_can_resolve_low_machine_confidence_without_inventing_grounding() -> None:
    from app.eval.review_dataset import assemble_review_dataset

    case = reviewed_case()
    case.signal.confidence = 0.35
    dataset = assemble_review_dataset([case])
    assert dataset.report.exported == 1
    assert dataset.records[0]["machine_grounding"]["issues"] == ["low_confidence"]
    case.signal.budget_krw = 123_000_000
    assert assemble_review_dataset([case]).report.exported == 0


def test_rejected_member_question_is_usable_negative_not_machine_positive() -> None:
    from app.eval.review_dataset import assemble_review_dataset

    quote = "스마트쉘터를 설치해 주십시오."
    case = reviewed_case(status="rejected", text=f"○위원 김민수  {quote}", quote=quote)
    dataset = assemble_review_dataset([case])
    assert dataset.report.exported == 1
    assert dataset.records[0]["human_label"]["decision"] == "reject"
    assert "official_evidence_missing" in dataset.records[0]["machine_grounding"]["issues"]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("source", "source_text_missing"),
        ("offset", "evidence_offsets_missing_or_invalid"),
        ("quote", "evidence_not_found"),
        ("positive_claim", "positive_grounding_insufficient"),
        ("timestamp", "review_timestamp_missing"),
        ("verdict", "review_signal_verdict_mismatch"),
    ],
)
def test_ineligible_review_is_excluded_with_explicit_reason(change: str, reason: str) -> None:
    from app.eval.review_dataset import assemble_review_dataset

    case = reviewed_case()
    if change == "source":
        case.document.text = None
    elif change == "offset":
        case.signal.evidence[0]["start"] = -1
    elif change == "quote":
        case.signal.evidence[0]["quote"] = "완전히 다른 사업의 근거 문장입니다."
    elif change == "positive_claim":
        case.signal.budget_krw = 123_000_000
    elif change == "timestamp":
        case.review.resolved_at = None
    else:
        case.signal.verdict = "rejected"
    dataset = assemble_review_dataset([case])
    assert dataset.report.exported == 0 and dataset.report.excluded == 1
    assert dataset.report.excluded_reasons[reason] == 1
    assert dataset.records == []


def test_shared_document_content_or_text_cannot_cross_splits() -> None:
    from app.eval.review_dataset import assemble_review_dataset

    a = reviewed_case(review_id=1, document_id=10, content_hash="a" * 64)
    b = reviewed_case(review_id=2, document_id=10, content_hash="a" * 64)
    c = reviewed_case(
        review_id=3, document_id=20, content_hash="a" * 64, text=TEXT + "\n다른 파서의 여백"
    )
    d = reviewed_case(review_id=4, document_id=30, content_hash="b" * 64)
    dataset = assemble_review_dataset([a, b, c, d])
    assert len({r["split"] for r in dataset.records}) == 1
    assert len({r["split_group"] for r in dataset.records}) == 1
    assert dataset.report.exported == 4


def test_repeated_export_is_byte_identical_and_digest_covers_sample_lines() -> None:
    from app.eval.review_dataset import assemble_review_dataset

    a, b = reviewed_case(review_id=2), reviewed_case(review_id=1)
    first = assemble_review_dataset([a, b])
    second = assemble_review_dataset([b, a])
    assert first.jsonl_bytes() == second.jsonl_bytes()
    manifest_line, sample_lines = first.jsonl_bytes().split(b"\n", 1)
    manifest = json.loads(manifest_line)
    assert manifest["record_type"] == "review_dataset_manifest"
    assert manifest["digest_scope"] == "sample_lines"
    assert manifest["digest"] == hashlib.sha256(sample_lines).hexdigest()
    assert manifest["temporal_scope"] == "current_stored_snapshot"
    b.review.resolution = {
        "action": "approve",
        "changes": {"title": {"from": "이전", "to": "새 제목"}},
    }
    b.review.status = "edited"
    b.signal.title = "새 제목"
    third = assemble_review_dataset([a, b])
    assert third.report.digest != first.report.digest


def test_resolution_history_exports_only_actor_time_action_and_known_edits() -> None:
    from app.eval.review_dataset import assemble_review_dataset

    case = reviewed_case(status="edited")
    case.review.resolution = {
        "action": "approve",
        "changes": {"budget_krw": {"from": 1, "to": None}, "email": {"to": "private@example.test"}},
        "history": [
            {
                "action": "reject",
                "resolved_by": 9,
                "resolved_at": "2026-09-27T00:00:00+00:00",
                "changes": {},
                "note": "SECRET_HISTORY",
            }
        ],
    }
    row = assemble_review_dataset([case]).records[0]
    assert row["human_label"]["revision"] == 2
    assert row["human_label"]["reviewed_fields"] == ["budget_krw"]
    assert row["human_label"]["history"][0]["actor_user_id"] == 9
    assert row["human_label"]["history"][0]["action"] == "reject"
    assert "SECRET_HISTORY" not in json.dumps(row)
    assert "email" not in row["human_label"]["changes"]


@pytest.mark.parametrize("seed", ["", " ", "a/b", "x" * 65])
def test_invalid_split_seed_is_rejected(seed: str) -> None:
    from app.eval.review_dataset import assemble_review_dataset

    with pytest.raises(ValueError, match="split_seed"):
        assemble_review_dataset([], split_seed=seed)


def test_category_is_reviewed_only_when_explicitly_edited() -> None:
    from app.eval.review_dataset import assemble_review_dataset

    case = reviewed_case(status="edited")
    case.signal.category = "mobility"
    case.review.resolution = {
        "action": "approve",
        "changes": {"category": {"from": "smart_city", "to": "mobility"}},
    }
    row = assemble_review_dataset([case]).records[0]
    assert row["human_label"]["reviewed_fields"] == ["category"]
    assert row["human_label"]["gold_standard"] is False


def test_atomic_writer_streams_and_preserves_previous_file_on_failure(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    from app.eval.review_dataset import ReviewDataset, _write_atomic, assemble_review_dataset

    dataset = assemble_review_dataset([reviewed_case()])
    target = tmp_path / "reviews.jsonl"
    _write_atomic(target, dataset)
    original = target.read_bytes()
    assert len(original.splitlines()) == 2

    def broken_lines(self):  # type: ignore[no-untyped-def]
        yield b'{"record_type":"review_dataset_manifest"}\n'
        raise OSError("interrupted export")

    monkeypatch.setattr(ReviewDataset, "iter_jsonl_bytes", broken_lines)
    with pytest.raises(OSError, match="interrupted export"):
        _write_atomic(target, dataset)
    assert target.read_bytes() == original
    assert list(tmp_path.iterdir()) == [target]
