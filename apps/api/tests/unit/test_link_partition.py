"""Canonical planning changes automatic membership, independent of incoming row order."""

from datetime import date, timedelta
from itertools import permutations
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.models import Opportunity, Signal
from app.pipeline import link
from app.pipeline.link import refresh_opportunity
from app.pipeline.link_partition import plan_partition, stable_signal_key

TODAY = date(2026, 9, 29)


def signal(
    number, *, stage="order_plan", refs=None, document=None, title="청사 냉난방기 교체", day=0
):
    return Signal(
        id=number,
        document_id=document or number,
        institution_code="A",
        title=title,
        summary="공공청사 시설개선",
        stage=stage,
        category="facility",
        observed_at=TODAY + timedelta(days=day),
        verdict="accepted",
        budget_krw=100_000_000,
        embedding=[1.0, 0.0],
        external_refs=refs or {},
        keywords=["청사"],
        evidence=[],
    )


def partition(rows, keys=None):
    keys = keys or {row.id: f"key-{row.id:03}" for row in rows}
    return plan_partition(rows, keys, threshold=0.6, review_band=0.08, today=TODAY)


def members(groups):
    return sorted(sorted(row.id for row in group.members) for group in groups)


def test_permutations_produce_same_partition_and_do_not_mutate_signal_inputs():
    rows = [
        signal(1, stage="budget_line", day=-20),
        signal(2, refs={"order_plan_no": "P"}),
        signal(3, stage="bid_notice", refs={"order_plan_no": "P", "bid_notice_no": "B1"}, day=1),
        signal(4, stage="bid_notice", refs={"order_plan_no": "P", "bid_notice_no": "B2"}, day=2),
    ]
    before = [(row.title, row.stage, tuple(row.embedding)) for row in rows]
    for order in permutations(rows):
        groups = partition(order)
        assert members(groups) == [[1, 2, 3], [4]]
        assert [(row.title, row.stage, tuple(row.embedding)) for row in rows] == before
        assert all(
            decision.opportunity_id == group.index
            for group in groups
            for decision in group.decisions.values()
        )


def test_late_competitor_retracts_an_earlier_reference_attachment_on_reconciliation():
    first = signal(1, refs={"order_plan_no": "P"}, day=-2)
    late = signal(2, refs={"prespec_no": "S"}, day=-1)
    # Separate strong purchase identity prevents similarity from hiding the ambiguity.
    first.external_refs["bid_notice_no"] = "B1"
    late.external_refs["bid_notice_no"] = "B2"
    bridge = signal(3, refs={"order_plan_no": "P", "prespec_no": "S"})
    assert members(partition([first, bridge])) == [[1, 3]]
    assert members(partition([bridge, late, first])) == [[1], [2], [3]]


def test_reference_revisions_and_cancellation_keep_own_bid_among_shared_plan():
    rows = [
        signal(1, stage="bid_notice", refs={"order_plan_no": "P", "bid_notice_no": "B1"}),
        signal(2, stage="bid_notice", refs={"order_plan_no": "P", "bid_notice_no": "B2"}),
        signal(3, stage="bid_notice", refs={"order_plan_no": "P", "cancels_bid_notice_no": "B1"}),
    ]
    assert members(partition(rows)) == [[1, 3], [2]]


def test_same_book_and_different_budget_names_remain_distinct():
    rows = [
        signal(1, stage="budget_line", document=10),
        signal(2, stage="budget_line", document=10),
        signal(3, stage="budget_line", document=11, title="판교도서관 냉난방기 교체"),
    ]
    assert members(partition(rows)) == [[1], [2], [3]]


def test_equal_dates_use_stable_keys_not_database_ids_for_summary_ties():
    older_id = signal(10, title="청사 냉난방기 교체 1", refs={"order_plan_no": "P"})
    newer_id = signal(999, title="청사 냉난방기 교체 2", refs={"order_plan_no": "P"})
    keys = {10: "z", 999: "a"}
    group = partition([older_id, newer_id], keys)[0]
    assert group.summary.title == older_id.title
    assert group.key == "a"
    older_id.id, newer_id.id = 5000, 1
    rekeyed = partition([newer_id, older_id], {5000: "z", 1: "a"})[0]
    assert rekeyed.summary.title == group.summary.title
    assert rekeyed.summary.embedding == group.summary.embedding


def test_provenance_key_ignores_database_ids_and_dedupe_but_includes_source_location():
    row = signal(1)
    metadata = dict(source_key="budget", document_external_id="2026", document_type="budget_book")
    original = stable_signal_key(row, **metadata, chunk_seq=1)
    row.id, row.document_id, row.chunk_id, row.dedupe_key = 900, 800, 700, "new-id-derived-hash"
    assert stable_signal_key(row, **metadata, chunk_seq=1) == original
    assert stable_signal_key(row, **metadata, chunk_seq=2) != original
    row.title += " 수정"
    assert stable_signal_key(row, **metadata, chunk_seq=1) != original


def test_identical_observations_are_reported_without_database_id_tie_break():
    rows = [
        signal(1, stage="budget_line", document=10),
        signal(2, stage="budget_line", document=10),
    ]
    groups = partition(rows, {1: "same", 2: "same"})
    assert len(groups) == 2
    assert all(group.ambiguous_keys == ("same",) for group in groups)


def test_planner_refuses_unaccepted_universe_and_missing_keys():
    row = signal(1)
    row.verdict = "rejected"
    with pytest.raises(ValueError, match="accepted"):
        partition([row])
    row.verdict = "accepted"
    with pytest.raises(ValueError, match="stable key"):
        partition([row], {5: "missing"})


def test_structural_gates_do_not_hide_the_thirteenth_eligible_candidate():
    rows = [
        signal(i, refs={"order_plan_no": f"OTHER-{i}", "bid_notice_no": f"B-{i}"})
        for i in range(1, 13)
    ]
    rows.extend([signal(13), signal(14, refs={"order_plan_no": "NEW"})])
    groups = partition(rows)
    assert len(groups) == 13
    assert [13, 14] in members(groups)


async def test_later_database_refresh_preserves_canonical_summary_ties():
    rows = [
        signal(10, title="청사 냉난방기 교체 1", refs={"order_plan_no": "P"}),
        signal(999, title="청사 냉난방기 교체 2", refs={"order_plan_no": "P"}),
    ]
    keys = {
        row.id: stable_signal_key(
            row,
            source_key="source",
            document_external_id=str(row.document_id),
            document_type="order_plan",
        )
        for row in rows
    }
    expected = partition(rows, keys)[0].summary
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: list(reversed(rows)))),
        execute=AsyncMock(
            return_value=SimpleNamespace(
                all=lambda: [
                    (row.id, "source", str(row.document_id), "order_plan", None, None, None, None)
                    for row in rows
                ]
            )
        ),
    )
    refreshed = Opportunity(id=123, institution_code="A")
    await refresh_opportunity(session, refreshed, today=TODAY)
    assert refreshed.title == expected.title
    assert refreshed.category == expected.category
    assert refreshed.embedding == expected.embedding
    assert refreshed.keywords == expected.keywords
    assert session.execute.await_count == 1  # one metadata query, never one query per signal


async def test_protected_reference_abstains_without_similarity_fallback(monkeypatch):
    incoming = signal(1, refs={"order_plan_no": "P"})
    protected = Opportunity(id=20)
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [protected]))
    )
    candidates = AsyncMock()
    monkeypatch.setattr(link, "_candidates", candidates)
    runtime = SimpleNamespace(settings=SimpleNamespace(link_threshold=0.6, link_review_band=0.08))
    result = await link.decide(session, runtime, incoming, blocked_opportunity_ids={20})
    assert result is None
    candidates.assert_not_awaited()


async def test_protected_similarity_targets_are_removed_before_scoring():
    incoming = signal(1)
    protected, automatic = Opportunity(id=20), Opportunity(id=21)
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [protected, automatic]))
    )
    assert await link._candidates(session, incoming, blocked_opportunity_ids={20}) == [automatic]
