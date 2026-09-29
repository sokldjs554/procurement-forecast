"""A future audit needs actual frozen inputs, not reconstructed vectors or historical claims."""

import hashlib
import json
from datetime import UTC, date, datetime

import pytest

from app.db.models import (
    Document,
    DocumentChunk,
    InstitutionRow,
    Opportunity,
    OpportunitySignal,
    Signal,
)
from app.settings import Settings

CAPTURED = datetime(2026, 9, 29, 12, tzinfo=UTC)
TEXT = "○교통과장 이민수  스마트쉘터를 설치할 계획입니다."


def graph():  # type: ignore[no-untyped-def]
    from app.eval.forecast_snapshot import ForecastGraph

    doc = Document(
        id=3,
        source_id=1,
        doc_type="council_minutes",
        title="회의록",
        text=TEXT,
        institution_code="LG-41130",
        published_at=date(2025, 1, 1),
        content_hash="a" * 64,
        parse_status="parsed",
        parse_method="text",
        created_at=CAPTURED,
        updated_at=CAPTURED,
        extracted_at=CAPTURED,
        structured={
            "meeting_date": "2025-01-01",
            "published_from": "meeting_date",
            "truth_id": "NEVER_EXPORT_TRUTH",
            "api_key": "STRUCTURED_SECRET",
        },
        raw_uri="s3://private?token=RAW_SECRET",
        url="https://source/?key=URL_SECRET",
    )
    signal = Signal(
        id=5,
        document_id=3,
        chunk_id=4,
        stage="council_mention",
        institution_code="LG-41130",
        title="스마트쉘터 설치",
        summary="설치 계획",
        category="smart_city",
        keywords=["스마트쉘터"],
        confidence=0.95,
        commitment="planned",
        observed_at=date(2025, 1, 1),
        extractor="heuristic-v1",
        grounding={"verdict": "accepted", "issues": [], "secret": "GROUNDING_SECRET"},
        evidence=[
            {
                "quote": "스마트쉘터를 설치할 계획입니다.",
                "start": 11,
                "end": len(TEXT),
                "found": True,
            }
        ],
        verdict="accepted",
        embedding=[0.25, -0.5],
        external_refs={},
        created_at=CAPTURED,
    )
    opp = Opportunity(
        id=6,
        institution_code="LG-41130",
        title="스마트쉘터",
        category="smart_city",
        stage="council_mention",
        status="open",
        first_seen_at=date(2025, 1, 1),
        last_signal_at=date(2025, 1, 1),
        bid_window_start=date(2026, 10, 1),
        bid_window_end=date(2027, 3, 1),
        conversion_prob=0.41,
        signal_count=1,
        embedding=[0.5, 0.5],
        keywords=["스마트쉘터"],
        created_at=CAPTURED,
        updated_at=CAPTURED,
    )
    chunk = DocumentChunk(
        id=4,
        document_id=3,
        seq=0,
        char_start=0,
        char_end=len(TEXT),
        text=TEXT,
        labels=["교통과장 이민수"],
        triage_passed=True,
        triage_score=0.9,
    )
    link = OpportunitySignal(
        opportunity_id=6,
        signal_id=5,
        score=0.9,
        method="similarity",
        tentative=False,
        reasons={"semantic": 0.8, "note": "LINK_SECRET"},
        created_at=CAPTURED,
    )
    institution = InstitutionRow(
        code="LG-41130",
        name="성남시",
        kind="local_gov",
        sido="경기도",
        region_code="41130",
        aliases=["성남시청"],
    )
    return ForecastGraph(
        documents=[doc],
        signals=[signal],
        opportunities=[opp],
        chunks=[chunk],
        links=[link],
        institutions=[institution],
    )


def assemble(g=None):  # type: ignore[no-untyped-def]
    from app.eval.forecast_snapshot import assemble_forecast_snapshot

    settings = Settings(
        env="test",
        embedding_dim=2,
        database_url="postgresql://SECRET_DATABASE",
        anthropic_api_key="SECRET_API_KEY",
    )
    return assemble_forecast_snapshot(
        g or graph(),
        institution_code="LG-41130",
        code_revision="abc1234",
        captured_at=CAPTURED,
        settings=settings,
    )


def test_snapshot_freezes_real_vectors_forecast_and_publication_provenance() -> None:
    snapshot = assemble()
    rows = list(snapshot.records)
    signal = next(row for row in rows if row.get("entity") == "signal")
    assert signal["data"]["embedding"]["values"] == [0.25, -0.5]
    assert signal["data"]["embedding"]["present"] is True
    prediction = next(row for row in rows if row["record_type"] == "prediction")
    assert prediction["data"]["conversion_prob"] == 0.41
    assert prediction["data"]["bid_window_start"] == "2026-10-01"
    document = next(row for row in rows if row.get("entity") == "document")
    assert document["data"]["text"] == TEXT
    assert document["data"]["text_sha256"] == hashlib.sha256(TEXT.encode()).hexdigest()
    assert document["data"]["structured"]["published_from"] == "meeting_date"
    manifest = json.loads(next(snapshot.iter_jsonl_bytes()))
    assert manifest["captured_at"] == "2026-09-29T12:00:00+00:00"
    assert manifest["temporal_scope"] == "current_observed_state_including_late_ingestion"
    assert manifest["historical_as_of_verified"] is False
    assert manifest["parsing_replay_possible"] is False
    assert manifest["code_revision_provenance"] == "operator_attested_not_git_verified"
    serialized = b"".join(snapshot.iter_jsonl_bytes()).decode()
    assert "SECRET" not in serialized and "NEVER_EXPORT_TRUTH" not in serialized


def test_missing_original_text_and_embedding_are_reported_not_rebuilt() -> None:
    state = graph()
    state.documents[0].text = None
    state.signals[0].embedding = None
    snapshot = assemble(state)
    assert snapshot.report.incomplete_reasons["document_text_missing"] == 1
    assert snapshot.report.incomplete_reasons["signal_embedding_missing"] == 1
    signal = next(row for row in snapshot.records if row.get("entity") == "signal")
    assert signal["data"]["embedding"] == {"present": False, "values": None, "dimension": None}


def test_payload_digest_changes_when_forecast_or_inputs_change() -> None:
    one = assemble()
    lines = list(one.iter_jsonl_bytes())
    assert json.loads(lines[0])["digest"] == hashlib.sha256(b"".join(lines[1:])).hexdigest()
    state = graph()
    state.opportunities[0].conversion_prob = 0.9
    assert assemble(state).report.digest != one.report.digest
    state = graph()
    state.signals[0].embedding = [0.3, -0.5]
    assert assemble(state).report.digest != one.report.digest
    assert assemble().report.digest == one.report.digest


def test_cap_failure_preserves_existing_file(tmp_path):  # type: ignore[no-untyped-def]
    from app.eval.forecast_snapshot import SnapshotLimitError, write_forecast_snapshot

    target = tmp_path / "snapshot.jsonl"
    target.write_text("previous snapshot")
    state = graph()
    additional = graph().signals[0]
    additional.id = 7
    state.signals.append(additional)
    with pytest.raises(SnapshotLimitError):
        write_forecast_snapshot(target, assemble(state), max_signals=1)
    assert target.read_text() == "previous snapshot"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("revision", ["", "not a revision", "https://private/?key=secret"])
def test_operator_attested_revision_must_be_nonempty_hex_identifier(revision: str) -> None:
    from app.eval.forecast_snapshot import assemble_forecast_snapshot

    with pytest.raises(ValueError, match="code_revision"):
        assemble_forecast_snapshot(
            graph(),
            institution_code="LG-41130",
            code_revision=revision,
            captured_at=CAPTURED,
            settings=Settings(env="test"),
        )
