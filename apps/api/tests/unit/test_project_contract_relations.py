"""Reviewing a relation requires current, attributable evidence on both endpoints."""

import hashlib
from datetime import date

import pytest

from app.db.models import Document, Opportunity, OpportunitySignal, Signal
from app.pipeline import relations


def evidence_world():
    project = Opportunity(id=1, institution_code="X", title="공원 정비 사업")
    contract = Opportunity(id=2, institution_code="X", title="공원 벤치 구매")
    rows = []
    for number, stage, doc_type, text in [
        (1, "budget_line", "budget_book", "공원 정비 예산 100000000원"),
        (2, "bid_notice", "bid_notice", "공원 벤치 구매 입찰공고"),
    ]:
        signal = Signal(
            id=number,
            document_id=number,
            title=text,
            stage=stage,
            institution_code="X",
            observed_at=date(2026, 9, 29),
            verdict="accepted",
            evidence=[{"quote": text, "found": True, "start": 0, "end": len(text)}],
            grounding={},
            external_refs={},
            dedupe_key=str(number),
        )
        document = Document(
            id=number,
            title=text,
            doc_type=doc_type,
            text=text,
            parse_status="parsed",
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            published_at=date(2026, 9, 29),
        )
        membership = OpportunitySignal(opportunity_id=number, signal_id=number, tentative=False)
        rows.append((signal, document, membership))
    return project, contract, rows


def test_snapshot_has_both_original_evidence_quotes_and_content_identity():
    project, contract, rows = evidence_world()
    snapshots = relations.validate_evidence(project, contract, [1, 2], rows)
    assert [item["signal_id"] for item in snapshots] == [1, 2]
    assert snapshots[0]["evidence"][0]["source_quote"] == "공원 정비 예산 100000000원"
    assert snapshots[1]["document_content_hash"] == rows[1][1].content_hash
    assert snapshots[0]["fingerprint"]


@pytest.mark.parametrize(
    "defect",
    [
        "self",
        "institution",
        "source_institution",
        "rejected",
        "tentative",
        "missing_text",
        "false_quote",
        "wrong_role",
        "wrong_membership",
    ],
)
def test_invalid_evidence_cannot_confirm_relation(defect):
    project, contract, rows = evidence_world()
    if defect == "self":
        contract.id = project.id
    elif defect == "institution":
        contract.institution_code = "OTHER"
    elif defect == "source_institution":
        rows[0][1].institution_code = "OTHER"
    elif defect == "rejected":
        rows[0][0].verdict = "rejected"
    elif defect == "tentative":
        rows[1][2].tentative = True
    elif defect == "missing_text":
        rows[0][1].text = None
    elif defect == "false_quote":
        rows[0][0].evidence[0]["quote"] = "원문에 없는 별도 사업"
    elif defect == "wrong_role":
        rows[0][0].stage = "bid_notice"
    else:
        rows[0][2].opportunity_id = 999
    with pytest.raises(relations.RelationValidationError):
        relations.validate_evidence(project, contract, [1, 2], rows)


def test_source_change_invalidates_confirmation_without_erasing_snapshot():
    project, contract, rows = evidence_world()
    snapshots = relations.validate_evidence(project, contract, [1, 2], rows)
    relation = type(
        "Relation", (), {"evidence_signal_ids": [1, 2], "evidence_snapshot": snapshots}
    )()
    assert relations.validity_reasons(relation, project, contract, rows) == []
    rows[0][0].title = "검토 후 수정한 사업명"
    assert "evidence_changed" in relations.validity_reasons(relation, project, contract, rows)
    assert snapshots[0]["signal_title"] == "공원 정비 예산 100000000원"


def test_deleted_signal_is_stale_instead_of_silently_using_new_evidence():
    project, contract, rows = evidence_world()
    snapshots = relations.validate_evidence(project, contract, [1, 2], rows)
    relation = type(
        "Relation", (), {"evidence_signal_ids": [1, 2], "evidence_snapshot": snapshots}
    )()
    reasons = relations.validity_reasons(relation, project, contract, rows[1:])
    assert "missing_evidence:1" in reasons


@pytest.mark.parametrize("owner", ["X", "OTHER", None])
def test_council_source_uses_registered_executive_owner_on_write_and_read(owner):
    project, contract, rows = evidence_world()
    rows[0][0].stage = "council_mention"
    rows[0][1].doc_type = "council_minutes"
    rows[0][1].institution_code = "COUNCIL"
    owners = {"COUNCIL": owner}
    if owner != "X":
        with pytest.raises(relations.RelationValidationError, match="source_institution_mismatch"):
            relations.validate_evidence(project, contract, [1, 2], rows, institution_owners=owners)
        return
    snapshots = relations.validate_evidence(
        project, contract, [1, 2], rows, institution_owners=owners
    )
    relation = type(
        "Relation", (), {"evidence_signal_ids": [1, 2], "evidence_snapshot": snapshots}
    )()
    assert (
        relations.validity_reasons(relation, project, contract, rows, institution_owners=owners)
        == []
    )
    assert "source_institution_mismatch:1" in relations.validity_reasons(
        relation, project, contract, rows, institution_owners={"COUNCIL": "OTHER"}
    )
