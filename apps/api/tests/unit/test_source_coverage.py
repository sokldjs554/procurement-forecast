"""Configuration, stored data, and readable evidence are different coverage claims."""

from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from typing import Any

from app.db.models import IngestRun, InstitutionRow, Source


async def _records(*rows: Any) -> AsyncIterator[Any]:
    for row in rows:
        yield row


def _institution(code: str = "CN-41130") -> InstitutionRow:
    return InstitutionRow(code=code, name="경기도 성남시의회", kind="council")


async def test_configured_source_without_documents_is_not_collected() -> None:
    from app.sources.coverage import build_coverage_report

    source = Source(
        id=1,
        key="minutes_boards",
        adapter="crawler",
        enabled=True,
        config={"boards": [{"institution_code": "CN-41130", "url": "https://council.test"}]},
    )
    report = await build_coverage_report([_institution()], [source], _records(), [])
    row = report["institutions"][0]
    assert row["configured_sources"] == 1
    assert row["enabled_configured_sources"] == 1
    assert row["collected"] == 0
    assert row["raw_available"] == 0
    assert report["summary"]["institutions_collected"] == 0


async def test_coverage_checks_local_raw_and_distinguishes_remote_and_inferred_dates(
    tmp_path: Path,
) -> None:
    from app.sources.coverage import CoverageDocument, build_coverage_report

    raw = tmp_path / "source.txt"
    raw.write_text("실제 저장된 회의록")
    source = Source(
        id=1, key="clik_minutes", adapter="clik", enabled=True, config={}, consecutive_failures=2
    )
    records = _records(
        CoverageDocument("CN-41130", 1, raw.as_uri(), date(2026, 9, 1), "meeting_date", "031013"),
        CoverageDocument(
            "CN-41130", 1, (tmp_path / "missing").as_uri(), date(2026, 9, 2), "crawled", "031013"
        ),
        CoverageDocument(
            "CN-41130", 1, "gs://private/archive", date(2026, 9, 3), "fiscal_year", "031013"
        ),
        CoverageDocument("CN-41130", 1, None, date(2026, 9, 4), None, "031013"),
    )
    report = await build_coverage_report(
        [_institution()],
        [source],
        records,
        [
            IngestRun(
                source_id=1, status="failed", error="FatalSourceError: private provider message"
            )
        ],
    )
    row = report["institutions"][0]
    assert (row["collected"], row["raw_referenced"], row["raw_available"]) == (4, 3, 1)
    assert (row["raw_missing"], row["raw_unverified"]) == (2, 1)
    assert row["publication_provenance"] == {
        "meeting_date": 1,
        "crawled": 1,
        "fiscal_year": 1,
        "unspecified": 1,
    }
    assert row["first_published_at"] == "2026-09-01"
    assert row["last_published_at"] == "2026-09-04"
    assert (
        row["configured_sources"] == 0
    )  # An unbounded API scope is not a per-council configuration.
    health = report["sources"][0]
    assert health["latest_status"] == "failed"
    assert health["consecutive_failures"] == 2
    assert "private provider message" not in str(report)


async def test_clik_targets_use_observed_provider_mapping_without_inventing_national_coverage() -> (
    None
):
    from app.sources.coverage import CoverageDocument, build_coverage_report

    source = Source(
        id=1,
        key="clik_minutes",
        adapter="clik",
        enabled=False,
        config={"council_ids": ["031013", "999999"]},
    )
    report = await build_coverage_report(
        [_institution(), _institution("CN-11680")],
        [source],
        _records(CoverageDocument("CN-41130", 1, None, date(2026, 9, 1), "meeting_date", "031013")),
        [],
    )
    rows = {row["code"]: row for row in report["institutions"]}
    assert rows["CN-41130"]["configured_sources"] == 1
    assert rows["CN-41130"]["enabled_configured_sources"] == 0
    assert rows["CN-11680"]["configured_sources"] == 0
    assert report["sources"][0]["unmapped_targets"] == ["999999"]
    assert report["summary"]["institutions_configured"] == 1
    assert report["summary"]["institutions_collected"] == 1
    assert report["scope"] == "stored_evidence_inventory"


async def test_coverage_excludes_fixture_sources_and_synthetic_documents() -> None:
    from app.sources.coverage import CoverageDocument, build_coverage_report

    real = Source(id=1, key="minutes_boards", adapter="crawler", enabled=True, config={})
    fixture = Source(id=2, key="fixture_budget", adapter="crawler", enabled=True, config={})
    report = await build_coverage_report(
        [_institution()],
        [real, fixture],
        _records(
            CoverageDocument("CN-41130", 2, None, date(2026, 9, 1), None, None),
            CoverageDocument("CN-41130", 1, None, date(2026, 9, 1), None, None, synthetic=True),
            CoverageDocument(None, 1, None, date(2026, 9, 1), None, None),
        ),
        [],
    )
    assert report["institutions"][0]["collected"] == 0
    assert report["unresolved"]["collected"] == 1
    assert report["summary"]["collected"] == 1
    assert [source["key"] for source in report["sources"]] == ["minutes_boards"]


async def test_inline_provider_json_counts_as_retained_evidence() -> None:
    from app.sources.coverage import CoverageDocument, build_coverage_report

    source = Source(id=1, key="g2b_bid", adapter="g2b", enabled=True, config={})
    report = await build_coverage_report(
        [_institution()],
        [source],
        _records(
            CoverageDocument("CN-41130", 1, None, date(2026, 9, 1), None, None, has_inline_raw=True)
        ),
        [],
    )
    row = report["institutions"][0]
    assert row["raw_referenced"] == 1
    assert row["raw_available"] == 1
    assert row["raw_inline"] == 1
    assert row["publication_provenance"] == {"unspecified": 1}


async def test_no_raw_check_does_not_turn_a_storage_reference_into_available(
    tmp_path: Path,
) -> None:
    from app.sources.coverage import CoverageDocument, build_coverage_report

    raw = tmp_path / "source.txt"
    raw.write_text("보존자료")
    source = Source(id=1, key="minutes_boards", adapter="crawler", enabled=True, config={})
    report = await build_coverage_report(
        [_institution()],
        [source],
        _records(CoverageDocument("CN-41130", 1, raw.as_uri(), date(2026, 9, 1), None, None)),
        [],
        verify_raw=False,
    )
    row = report["institutions"][0]
    assert row["raw_available"] == 0
    assert row["raw_unverified"] == 1
