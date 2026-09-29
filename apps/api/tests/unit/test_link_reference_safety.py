"""Reference identity and retained history must survive conservative relinking."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.models import Opportunity, Signal
from app.pipeline.link import _without_conflicting_numbers, decide, refresh_opportunity

TODAY = date(2026, 9, 29)


def opportunity(id=1):
    return Opportunity(
        id=id,
        institution_code="TEST",
        title="청사 냉난방기 교체",
        category="facility",
        stage="bid_notice",
        status="bid_open",
        first_seen_at=date(2026, 1, 1),
        last_signal_at=TODAY,
        bid_published_at=TODAY,
        bid_window_start=TODAY,
        bid_window_end=TODAY,
        est_budget_krw=100_000_000,
        best_commitment="committed",
        signal_count=3,
        conversion_prob=1.0,
        keywords=["냉난방기"],
        embedding=[1.0] + [0.0] * 511,
    )


@pytest.mark.parametrize(
    ("incoming", "existing"),
    [
        ({"bid_notice_no": "B2"}, {"bid_notice_no": "B1"}),
        (
            {"order_plan_no": "P", "bid_notice_no": "B2"},
            {"order_plan_no": "P", "bid_notice_no": "B1"},
        ),
        ({"bid_notice_nos": ["B1", "B2"]}, {"bid_notice_no": "B1"}),
        ({"bid_notice_no": "B1"}, {"bid_notice_nos": ["B1", "B2"]}),
        ({"bid_notice_nos": ["B2"]}, {"bid_notice_no": "B1"}),
    ],
)
async def test_different_or_multiple_bid_identities_cannot_merge(incoming, existing):
    session = SimpleNamespace(execute=AsyncMock(return_value=[(1, existing)]))
    signal = Signal(external_refs=incoming)
    assert await _without_conflicting_numbers(session, signal, [opportunity()]) == []


@pytest.mark.parametrize(
    "incoming",
    [
        {"bid_notice_no": "B1", "bid_notice_ord": "001"},
        {"bid_notice_no": "B1", "cancels_bid_notice_no": "B1"},
        {"bid_notice_nos": ["B1"]},
    ],
)
async def test_revision_cancellation_and_single_bid_plan_keep_identity(incoming):
    session = SimpleNamespace(execute=AsyncMock(return_value=[(1, {"bid_notice_no": "B1"})]))
    opp = opportunity()
    assert await _without_conflicting_numbers(session, Signal(external_refs=incoming), [opp]) == [
        opp
    ]


async def test_reference_ambiguity_abstains_before_similarity():
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=1),
        scalars=AsyncMock(
            return_value=SimpleNamespace(all=lambda: [opportunity(1), opportunity(2)])
        ),
    )
    signal = Signal(
        id=3, institution_code="TEST", external_refs={"order_plan_no": "P", "prespec_no": "S"}
    )
    runtime = SimpleNamespace(settings=SimpleNamespace(link_threshold=0.6, link_review_band=0.08))
    assert await decide(session, runtime, signal) is None


async def test_bid_revision_identity_wins_over_a_shared_plan():
    first, second = opportunity(1), opportunity(2)
    session = SimpleNamespace(
        scalars=AsyncMock(
            side_effect=[
                SimpleNamespace(all=lambda: [first, second]),
                SimpleNamespace(all=lambda: [first]),
            ]
        ),
        execute=AsyncMock(return_value=[(1, {"bid_notice_no": "B1", "order_plan_no": "P"})]),
    )
    signal = Signal(
        id=3,
        institution_code="TEST",
        external_refs={
            "order_plan_no": "P",
            "bid_notice_no": "B1",
            "cancels_bid_notice_no": "B1",
        },
    )
    runtime = SimpleNamespace(settings=SimpleNamespace(link_threshold=0.6, link_review_band=0.08))
    result = await decide(session, runtime, signal)
    assert result is not None and result.opportunity_id == 1


async def test_empty_eligible_thread_keeps_history_without_active_summary():
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [])), delete=AsyncMock()
    )
    opp = opportunity()
    await refresh_opportunity(session, opp, today=TODAY)
    assert opp.status == "dormant"
    assert opp.signal_count == 0 and opp.conversion_prob == 0
    assert opp.est_budget_krw is None and opp.best_commitment is None
    assert opp.bid_published_at is None
    assert opp.bid_window_start is None and opp.bid_window_end is None
    assert opp.embedding is None and opp.keywords == []
    assert opp.id == 1 and opp.title == "청사 냉난방기 교체"
