"""Offline follow-up of byte-frozen forecasts; no DB relinking or historical reconstruction.

Snapshot hashes check byte identity, not the truth of an operator's capture timestamp. Outcome
matching and coverage are supplied attestations. Current v1 probabilities have no frozen target
horizon, so this evaluator deliberately does not present Brier scores or calibration claims.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.clock import KST, now_utc, today_kst
from app.domain.stages import CANCELS_KEY, Stage

METHOD_VERSION = "frozen-forecast-followup-v1"


def _date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO calendar date, not an unknown/null date")
    try:
        result = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO calendar date") from exc
    if result.isoformat() != value:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return result


def _datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a timezone-aware datetime")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a timezone-aware datetime") from exc
    if result.tzinfo is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return result


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be nonempty")
    return value


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def _rows(value: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return [_object(item) for item in value]


def _identity(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("opportunity_id must be a positive integer")
    return value


def _snapshot(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    if not lines:
        raise ValueError("Snapshot is empty")
    manifest = _object(json.loads(lines[0]))
    if (
        manifest.get("version") != "forecast-snapshot-v1"
        or manifest.get("record_type") != "forecast_snapshot_manifest"
        or manifest.get("digest_scope") != "following_record_lines"
        or manifest.get("prediction_origin") != "stored_not_recomputed"
    ):
        raise ValueError("Expected an original forecast-snapshot-v1 export")
    if manifest.get("digest") != hashlib.sha256(b"".join(lines[1:])).hexdigest():
        raise ValueError("Snapshot payload digest mismatch")
    records = [_object(json.loads(line)) for line in lines[1:]]
    if Counter(row.get("entity") for row in records) != manifest.get("counts"):
        raise ValueError("Snapshot entity counts mismatch")
    configs = [row for row in records if row.get("entity") == "configuration"]
    if len(configs) != 1:
        raise ValueError("Snapshot must contain one frozen configuration")
    config = _object(configs[0].get("data"))
    for key in ("captured_at", "code_revision"):
        if config.get(key) != manifest.get(key):
            raise ValueError(f"Snapshot manifest/configuration {key} mismatch")
    return manifest, records, hashlib.sha256(raw).hexdigest()


def _observations(
    path: Path | None,
    *,
    predictions: dict[int, dict[str, Any]],
    snapshot_sha256: str,
    captured_at: datetime,
    as_of: date,
) -> tuple[dict[str, Any], dict[int, dict[str, Any]], dict[int, list[date]], str | None]:
    if path is None:
        return {}, {}, {}, None
    raw = path.read_bytes()
    bundle = _object(json.loads(raw))
    if bundle.get("version") != "longitudinal-observations-v1":
        raise ValueError("Unsupported observations version")
    if bundle.get("snapshot_sha256") != snapshot_sha256:
        raise ValueError("Observations snapshot_sha256 does not match the complete snapshot file")
    if bundle.get("data_origin") not in ("real", "synthetic"):
        raise ValueError("data_origin must explicitly state real or synthetic")
    through = _date(bundle.get("observed_through"), "observed_through")
    if through > as_of or through < captured_at.astimezone(KST).date():
        raise ValueError("observed_through is outside the capture-to-evaluation interval")

    def match(row: dict[str, Any]) -> int:
        identity = _identity(row.get("opportunity_id"))
        if identity not in predictions:
            raise ValueError("Observation references an unknown frozen opportunity_id")
        if row.get("institution_code") != predictions[identity].get("institution_code"):
            raise ValueError("Observation institution differs from the frozen prediction")
        _text(row.get("evidence_ref"), "evidence_ref")
        return identity

    def observed(value: Any, field: str) -> date:
        at = _datetime(value, field)
        if at < captured_at or at > now_utc() or at.astimezone(KST).date() > through:
            raise ValueError(f"{field} is before capture or after the observation cutoff")
        return at.astimezone(KST).date()

    coverage: dict[int, dict[str, Any]] = {}
    for row in _rows(bundle.get("coverage"), "coverage"):
        identity = match(row)
        if identity in coverage:
            raise ValueError("Duplicate coverage for a frozen opportunity")
        first = _date(row.get("from_date"), "from_date")
        last = _date(row.get("through_date"), "through_date")
        recorded = observed(row.get("recorded_at"), "recorded_at")
        if first > last or last > through or last > recorded:
            raise ValueError("Coverage dates exceed the observation/recording cutoff")
        if type(row.get("complete")) is not bool:
            raise ValueError("Coverage complete must be an explicit boolean")
        coverage[identity] = row | {"first": first, "last": last}
    outcomes: dict[int, list[date]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for row in _rows(bundle.get("outcomes"), "outcomes"):
        identity = match(row)
        notice = _text(row.get("bid_notice_id"), "bid_notice_id")
        _text(row.get("match_basis"), "match_basis")
        if row.get("kind") not in ("bid_notice", "cancellation"):
            raise ValueError("Outcome kind must be bid_notice or cancellation")
        # A cancellation revision may share the original notice number; duplicate original
        # notices and one notice assigned to several projects require adjudication first.
        key = (notice, row["kind"])
        if key in seen:
            raise ValueError("Duplicate or ambiguously assigned bid_notice_id")
        seen.add(key)
        if row.get("publication_basis") != "official_publication":
            raise ValueError("publication_basis must be official_publication, not an inferred date")
        published = _date(row.get("published_on"), "published_on")
        observed_on = observed(row.get("observed_at"), "observed_at")
        if published > observed_on:
            raise ValueError("Tender publication is later than its observation")
        if row["kind"] == "bid_notice":
            outcomes[identity].append(published)
    return bundle, coverage, outcomes, hashlib.sha256(raw).hexdigest()


def _known_bids(records: list[dict[str, Any]]) -> set[int]:
    signals = {row["data"]["id"]: row["data"] for row in records if row.get("entity") == "signal"}
    known: set[int] = set()
    for row in records:
        if row.get("entity") != "link":
            continue
        link = _object(row.get("data"))
        signal = signals.get(link.get("signal_id"), {})
        if (
            link.get("tentative") is False
            and signal.get("verdict") == "accepted"
            and signal.get("stage") in (Stage.BID.value, Stage.AWARD.value)
            and CANCELS_KEY not in signal.get("external_refs", {})
        ):
            known.add(_identity(link.get("opportunity_id")))
    return known


def _covered(row: dict[str, Any], start: date, end: date) -> bool:
    return bool(row.get("complete") and row["first"] <= start and row["last"] >= end)


def evaluate_longitudinal(
    snapshot_path: Path,
    observations_path: Path | None = None,
    *,
    horizon_days: int = 540,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Evaluate all frozen opportunities against supplied later observations, without mutation.

    Missing observations never imply failure. Both known successes and unknown outcomes must
    have a mature, complete follow-up interval before entering a rate's denominator. All dates
    use KST; same-capture-day tenders are excluded because their ordering is not established.
    """
    as_of = as_of or today_kst()
    if as_of > today_kst():
        raise ValueError("A future cutoff cannot establish elapsed observation time")
    if type(horizon_days) is not int or horizon_days <= 0:
        raise ValueError("horizon_days must be a positive integer")
    manifest, records, snapshot_hash = _snapshot(Path(snapshot_path))
    captured_at = _datetime(manifest.get("captured_at"), "captured_at")
    captured = captured_at.astimezone(KST).date()
    if captured > as_of or captured_at > now_utc():
        raise ValueError("Snapshot capture is after the evaluation cutoff")
    predictions: dict[int, dict[str, Any]] = {}
    for row in records:
        if row.get("record_type") != "prediction":
            continue
        if row.get("entity") != "opportunity":
            raise ValueError("Unsupported prediction entity")
        data = _object(row.get("data"))
        identity = _identity(data.get("id"))
        if identity in predictions:
            raise ValueError("Duplicate frozen opportunity id")
        _text(data.get("institution_code"), "institution_code")
        predictions[identity] = data
    bundle, coverage, outcomes, observation_hash = _observations(
        Path(observations_path) if observations_path is not None else None,
        predictions=predictions,
        snapshot_sha256=snapshot_hash,
        captured_at=captured_at,
        as_of=as_of,
    )
    known_bids = _known_bids(records)
    counts: Counter[str] = Counter()
    window_exclusions: Counter[str] = Counter()
    positive_exclusions: Counter[str] = Counter()
    results: list[dict[str, Any]] = []
    end = captured + timedelta(days=horizon_days)
    start = captured + timedelta(days=1)
    through = _date(bundle["observed_through"], "observed_through") if bundle else None
    conversions: list[int] = []
    window_hits: list[int] = []
    lead_days: list[int] = []
    for identity, prediction in sorted(predictions.items()):
        stage = prediction.get("stage")
        if stage not in {value.value for value in Stage}:
            raise ValueError("Unknown frozen prediction stage")
        first_bid = min(outcomes[identity]) if outcomes.get(identity) else None
        row_result: dict[str, Any] = {"opportunity_id": identity}
        if (
            stage in (Stage.BID.value, Stage.AWARD.value)
            or prediction.get("bid_published_at") is not None
            or identity in known_bids
        ):
            status = "already_published"
        elif first_bid is not None and first_bid <= captured:
            status = "outcome_predates_freeze"
        else:
            counts["eligible"] += 1
            converted = int(first_bid is not None and first_bid <= end)
            row_result["observed_positive_within_horizon"] = bool(converted)
            covered = coverage.get(identity, {})
            if end > as_of or (through is not None and end > through):
                status = "right_censored"
            elif not _covered(covered, start, end):
                status = "coverage_incomplete"
            else:
                status = "evaluated"
                conversions.append(converted)
                if converted and first_bid is not None:
                    lead_days.append((first_bid - captured).days)
            if converted and status != "evaluated":
                positive_exclusions[status] += 1

            window_start = prediction.get("bid_window_start")
            window_end = prediction.get("bid_window_end")
            if window_start is None or window_end is None:
                window_exclusions["window_missing"] += 1
            else:
                lo, hi = (
                    _date(window_start, "bid_window_start"),
                    _date(window_end, "bid_window_end"),
                )
                if lo > hi:
                    raise ValueError("Frozen bid window start is after its end")
                if lo <= captured:
                    window_exclusions["window_not_future_at_freeze"] += 1
                elif hi > as_of or (through is not None and hi > through):
                    window_exclusions["right_censored"] += 1
                elif not _covered(covered, start, hi):
                    window_exclusions["coverage_incomplete"] += 1
                else:
                    window_hits.append(int(first_bid is not None and lo <= first_bid <= hi))
        counts[status] += 1
        results.append(row_result | {"status": status})
    return {
        "method_version": METHOD_VERSION,
        "evaluation_scope": "operator_attested_frozen_forecast_followup",
        "as_of": as_of.isoformat(),
        "captured_at": captured_at.isoformat(),
        "horizon_days": horizon_days,
        "horizon_end": end.isoformat(),
        "observed_through": through.isoformat() if through else None,
        "data_origin": bundle.get("data_origin", "unverified"),
        "lineage": {
            "snapshot_sha256": snapshot_hash,
            "snapshot_payload_sha256": manifest["digest"],
            "observations_sha256": observation_hash,
            "snapshot_code_revision": manifest["code_revision"],
            "snapshot_incomplete_reasons": manifest.get("incomplete_reasons", {}),
            "capture_time_independently_verified": False,
            "coverage_and_outcome_matching_independently_verified": False,
        },
        "frozen_predictions": len(predictions),
        "cohort_counts": dict(sorted(counts.items())),
        "observed_positives_excluded_from_rates": dict(sorted(positive_exclusions.items())),
        "conversion_within_horizon": {
            "n": len(conversions),
            "events": sum(conversions),
            "rate": sum(conversions) / len(conversions) if conversions else None,
        },
        "bid_window_hits": {
            "n": len(window_hits),
            "hits": sum(window_hits),
            "rate": sum(window_hits) / len(window_hits) if window_hits else None,
        },
        "window_exclusions": dict(sorted(window_exclusions.items())),
        "lead_days_from_freeze": {
            "n": len(lead_days),
            "median": statistics.median(lead_days) if lead_days else None,
        },
        "probability_calibration": {
            "available": False,
            "reason": "snapshot_v1_probability_target_and_horizon_not_frozen",
        },
        "opportunities": results,
        "limitations": [
            "Digests verify bytes, not trustworthy capture timestamps or independent storage.",
            "Outcome matching and exhaustive coverage are operator attestations, not verified truth.",
            "Rates describe the supplied frozen cohort, not nationwide procurement accuracy or recall.",
            "Capture-day tenders are excluded because date-only publication cannot prove ordering.",
            "Lead time starts at freeze, not an inferred fiscal-year, meeting or document date.",
            "Conversion means first tender publication, not an active tender, award or fulfilled contract.",
            "Snapshot v1 probabilities have no frozen horizon; calibration/Brier metrics are unavailable.",
        ],
    }
