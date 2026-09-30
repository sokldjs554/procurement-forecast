"""Synthetic boundary fixtures test auditing mechanics, never real forecast accuracy."""

import hashlib
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest


def snapshot(tmp_path: Path, **updates: Any) -> Path:
    predictions = [
        {
            "record_type": "prediction",
            "entity": "opportunity",
            "data": {
                "id": identity,
                "institution_code": "LG-41130",
                "stage": "budget_line",
                "status": "open",
                "bid_published_at": None,
                "bid_window_start": "2024-01-02",
                "bid_window_end": "2024-01-11",
                "conversion_prob": 0.7,
                **updates,
            },
        }
        for identity in (1, 2)
    ]
    records = [
        {
            "record_type": "frozen_input",
            "entity": "configuration",
            "data": {"captured_at": "2024-01-01T12:00:00+00:00", "code_revision": "abc1234"},
        },
        *predictions,
    ]
    body = b"".join((json.dumps(row) + "\n").encode() for row in records)
    manifest = {
        "record_type": "forecast_snapshot_manifest",
        "version": "forecast-snapshot-v1",
        "captured_at": "2024-01-01T12:00:00+00:00",
        "code_revision": "abc1234",
        "institution_code": "LG-41130",
        "digest_scope": "following_record_lines",
        "digest": hashlib.sha256(body).hexdigest(),
        "counts": {"configuration": 1, "opportunity": 2},
        "incomplete_reasons": {},
        "prediction_origin": "stored_not_recomputed",
    }
    path = tmp_path / "snapshot.jsonl"
    path.write_bytes((json.dumps(manifest) + "\n").encode() + body)
    return path


def evidence(path: Path) -> dict[str, Any]:
    return {
        "version": "longitudinal-observations-v1",
        "snapshot_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "data_origin": "synthetic",
        "observed_through": "2024-01-12",
        "coverage": [
            {
                "opportunity_id": identity,
                "institution_code": "LG-41130",
                "from_date": "2024-01-02",
                "through_date": "2024-01-12",
                "complete": True,
                "evidence_ref": "synthetic complete census",
                "recorded_at": "2024-01-12T12:00:00+00:00",
            }
            for identity in (1, 2)
        ],
        "outcomes": [
            {
                "opportunity_id": 1,
                "institution_code": "LG-41130",
                "bid_notice_id": "synthetic-bid-1",
                "kind": "bid_notice",
                "published_on": "2024-01-06",
                "publication_basis": "official_publication",
                "observed_at": "2024-01-07T00:00:00+00:00",
                "evidence_ref": "synthetic notice source",
                "match_basis": "independent adjudication of frozen project scope",
            }
        ],
    }


def measure(
    path: Path, bundle: dict[str, Any] | None = None, *, as_of: str = "2024-01-12"
) -> dict[str, Any]:
    from app.eval.longitudinal import evaluate_longitudinal

    observations = path.parent / "observations.json"
    if bundle is not None:
        observations.write_text(json.dumps(bundle))
    return evaluate_longitudinal(
        path,
        observations if bundle is not None else None,
        horizon_days=10,
        as_of=date.fromisoformat(as_of),
    )


def test_mature_complete_followup_uses_frozen_window_and_reports_lineage(tmp_path: Path) -> None:
    path = snapshot(tmp_path)
    report = measure(path, evidence(path))
    assert report["cohort_counts"] == {"eligible": 2, "evaluated": 2}
    assert report["conversion_within_horizon"] == {"n": 2, "events": 1, "rate": 0.5}
    assert report["bid_window_hits"] == {"n": 2, "hits": 1, "rate": 0.5}
    assert report["lead_days_from_freeze"] == {"n": 1, "median": 5}
    assert report["probability_calibration"]["available"] is False
    assert report["lineage"]["snapshot_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert report["lineage"]["snapshot_code_revision"] == "abc1234"
    assert report["data_origin"] == "synthetic"


def test_success_and_failure_are_equally_censored_before_horizon(tmp_path: Path) -> None:
    path = snapshot(tmp_path)
    bundle = evidence(path)
    bundle["observed_through"] = "2024-01-08"
    for coverage in bundle["coverage"]:
        coverage.update(through_date="2024-01-08", recorded_at="2024-01-08T12:00:00+00:00")
    report = measure(path, bundle, as_of="2024-01-08")
    assert report["cohort_counts"] == {"eligible": 2, "right_censored": 2}
    assert report["conversion_within_horizon"]["rate"] is None
    assert report["observed_positives_excluded_from_rates"] == {"right_censored": 1}


@pytest.mark.parametrize("change", ["missing", "incomplete", "gap"])
def test_absence_of_covered_outcomes_never_becomes_a_negative(tmp_path: Path, change: str) -> None:
    path = snapshot(tmp_path)
    bundle = evidence(path)
    if change == "missing":
        bundle["coverage"] = []
    elif change == "incomplete":
        for row in bundle["coverage"]:
            row["complete"] = False
    else:
        for row in bundle["coverage"]:
            row["from_date"] = "2024-01-03"
    report = measure(path, bundle)
    assert report["cohort_counts"] == {"eligible": 2, "coverage_incomplete": 2}
    assert report["conversion_within_horizon"]["n"] == 0
    assert report["observed_positives_excluded_from_rates"] == {"coverage_incomplete": 1}


def test_no_observation_file_reports_unavailable_even_for_old_snapshot(tmp_path: Path) -> None:
    report = measure(snapshot(tmp_path))
    assert report["cohort_counts"]["coverage_incomplete"] == 2
    assert report["conversion_within_horizon"]["rate"] is None


@pytest.mark.parametrize(("published", "expected"), [("2024-01-11", 1), ("2024-01-12", 0)])
def test_exact_horizon_boundary(tmp_path: Path, published: str, expected: int) -> None:
    path = snapshot(tmp_path)
    bundle = evidence(path)
    bundle["outcomes"][0].update(published_on=published, observed_at="2024-01-12T12:00:00+00:00")
    assert measure(path, bundle)["conversion_within_horizon"]["events"] == expected


def test_late_discovered_prefreeze_tender_excludes_stale_forecast(tmp_path: Path) -> None:
    path = snapshot(tmp_path)
    bundle = evidence(path)
    bundle["outcomes"][0]["published_on"] = "2024-01-01"
    report = measure(path, bundle)
    assert report["cohort_counts"]["outcome_predates_freeze"] == 1
    assert report["conversion_within_horizon"] == {"n": 1, "events": 0, "rate": 0.0}


@pytest.mark.parametrize("updates", [{"stage": "bid_notice"}, {"bid_published_at": "2023-12-20"}])
def test_existing_tenders_are_never_scored_as_forecasts(
    tmp_path: Path, updates: dict[str, Any]
) -> None:
    report = measure(snapshot(tmp_path, **updates))
    assert report["cohort_counts"] == {"already_published": 2}


def test_cancellation_record_does_not_invent_a_tender(tmp_path: Path) -> None:
    path = snapshot(tmp_path)
    bundle = evidence(path)
    bundle["outcomes"][0]["kind"] = "cancellation"
    assert measure(path, bundle)["conversion_within_horizon"]["events"] == 0


def test_tampered_snapshot_fails_closed(tmp_path: Path) -> None:
    path = snapshot(tmp_path)
    path.write_bytes(
        path.read_bytes().replace(b'"conversion_prob": 0.7', b'"conversion_prob": 0.9')
    )
    with pytest.raises(ValueError, match="digest"):
        measure(path)


def test_observations_for_different_snapshot_fail_closed(tmp_path: Path) -> None:
    path = snapshot(tmp_path)
    bundle = evidence(path)
    bundle["snapshot_sha256"] = "a" * 64
    with pytest.raises(ValueError, match="snapshot_sha256"):
        measure(path, bundle)


@pytest.mark.parametrize(
    "change",
    ["future", "naive", "foreign", "duplicate", "unmatched", "coverage_future", "unknown_kind"],
)
def test_invalid_outcome_evidence_fails_closed(tmp_path: Path, change: str) -> None:
    path = snapshot(tmp_path)
    bundle = evidence(path)
    outcome = bundle["outcomes"][0]
    if change == "future":
        outcome["observed_at"] = "2024-01-13T00:00:00+00:00"
    elif change == "naive":
        outcome["observed_at"] = "2024-01-07T00:00:00"
    elif change == "foreign":
        outcome["institution_code"] = "LG-other"
    elif change == "duplicate":
        bundle["outcomes"].append(dict(outcome))
    elif change == "unmatched":
        outcome["opportunity_id"] = 999
    elif change == "coverage_future":
        bundle["coverage"][0]["through_date"] = "2024-02-01"
    else:
        outcome["kind"] = "award"
    expected_errors = {
        "future": "observation cutoff",
        "naive": "timezone-aware",
        "foreign": "institution differs",
        "duplicate": "Duplicate",
        "unmatched": "unknown frozen",
        "coverage_future": "Coverage dates",
        "unknown_kind": "Outcome kind",
    }
    with pytest.raises(ValueError, match=expected_errors[change]):
        measure(path, bundle)


def test_future_cutoff_is_not_permission_to_claim_elapsed_followup(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="future"):
        measure(snapshot(tmp_path), as_of="2099-01-01")


@pytest.mark.parametrize("basis", ["meeting_date", "fiscal_year", "last_modified", None])
def test_inferred_dates_cannot_become_tender_publication_truth(
    tmp_path: Path, basis: str | None
) -> None:
    path = snapshot(tmp_path)
    bundle = evidence(path)
    bundle["outcomes"][0]["publication_basis"] = basis
    with pytest.raises(ValueError, match="publication_basis"):
        measure(path, bundle)


def test_expired_window_is_excluded_from_window_accuracy(tmp_path: Path) -> None:
    path = snapshot(tmp_path, bid_window_end="2023-12-31", bid_window_start="2023-12-01")
    report = measure(path, evidence(path))
    assert report["bid_window_hits"] == {"n": 0, "hits": 0, "rate": None}
    assert report["window_exclusions"] == {"window_not_future_at_freeze": 2}


def test_script_reads_snapshot_without_modifying_it(tmp_path: Path) -> None:
    path = snapshot(tmp_path)
    original = path.read_bytes()
    script = Path(__file__).resolve().parents[4] / "scripts" / "longitudinal-evaluate.py"
    result = subprocess.run(  # noqa: S603 - fixed repository script and controlled paths
        [sys.executable, str(script), "--snapshot", str(path), "--as-of", "2024-01-12"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["conversion_within_horizon"]["rate"] is None
    assert path.read_bytes() == original


def test_observation_before_freeze_fails_even_for_a_later_published_notice(tmp_path: Path) -> None:
    path = snapshot(tmp_path)
    bundle = evidence(path)
    bundle["outcomes"][0]["observed_at"] = "2023-12-30T00:00:00+00:00"
    with pytest.raises(ValueError, match="before capture"):
        measure(path, bundle)


def test_unknown_publication_date_cannot_become_a_negative(tmp_path: Path) -> None:
    path = snapshot(tmp_path)
    bundle = evidence(path)
    bundle["outcomes"][0]["published_on"] = None
    with pytest.raises(ValueError, match="unknown/null"):
        measure(path, bundle)


def test_manifest_backdating_without_configuration_change_fails(tmp_path: Path) -> None:
    path = snapshot(tmp_path)
    lines = path.read_bytes().splitlines(keepends=True)
    manifest = json.loads(lines[0])
    manifest["captured_at"] = "2023-01-01T12:00:00+00:00"
    path.write_bytes((json.dumps(manifest) + "\n").encode() + b"".join(lines[1:]))
    with pytest.raises(ValueError, match="captured_at mismatch"):
        measure(path)
