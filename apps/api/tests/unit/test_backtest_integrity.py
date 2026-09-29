"""Controlled records prove temporal boundaries without an external service."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.db.models import Document, Opportunity, Signal
from app.pipeline.backtest import run_backtest


def row(opp_id, stage, at, *, published=None, commitment="planned", provenance=None):  # type: ignore[no-untyped-def]
    return (
        Opportunity(id=opp_id, bid_published_at=date(2030, 1, 1), category="facility"),
        Signal(
            id=opp_id * 100 + at.day,
            stage=stage,
            observed_at=at,
            verdict="accepted",
            commitment=commitment,
            category="facility",
            external_refs={},
        ),
        Document(
            published_at=published or at,
            structured={"published_from": provenance} if provenance else {},
        ),
    )


async def measure(rows, today=date(2026, 9, 29), horizon_days=540):  # type: ignore[no-untyped-def]
    async def execute(query):  # type: ignore[no-untyped-def]
        has_documents = any(c["entity"] is Document for c in query.column_descriptions)
        assert has_documents
        projected = [
            SimpleNamespace(
                opportunity_id=opp.id,
                id=sig.id,
                stage=sig.stage,
                observed_at=sig.observed_at,
                commitment=sig.commitment,
                external_refs=sig.external_refs,
                published_at=doc.published_at,
                structured=doc.structured,
            )
            for opp, sig, doc in rows
        ]
        return SimpleNamespace(all=lambda: projected)

    session = SimpleNamespace(execute=AsyncMock(side_effect=execute))
    return await run_backtest(session, today=today, horizon_days=horizon_days)


async def test_future_tender_does_not_count_at_a_past_cutoff() -> None:
    result = await measure(
        [
            row(1, "council_mention", date(2024, 1, 1)),
            row(1, "bid_notice", date(2027, 1, 1)),
        ]
    )
    assert result["tender_early_coverage"]["tenders"] == 0
    assert result["conversion_by_first_signal"]["council_mention:planned"] == {"n": 1, "rate": 0.0}


async def test_young_successes_and_pending_projects_are_both_censored() -> None:
    result = await measure(
        [
            row(1, "council_mention", date(2026, 8, 1)),
            row(1, "bid_notice", date(2026, 9, 1)),
            row(2, "council_mention", date(2026, 8, 1)),
        ]
    )
    assert result["conversion_by_first_signal"] == {}
    assert result["censored_opportunities"] == 2


async def test_conversion_must_happen_inside_the_fixed_horizon() -> None:
    result = await measure(
        [
            row(1, "council_mention", date(2024, 1, 1)),
            row(1, "bid_notice", date(2026, 1, 1)),
        ]
    )
    assert result["conversion_by_first_signal"]["council_mention:planned"]["rate"] == 0.0


async def test_later_commitment_does_not_relabel_the_first_signal() -> None:
    result = await measure(
        [
            row(1, "council_mention", date(2024, 1, 1), commitment="reviewing"),
            row(1, "council_mention", date(2024, 2, 1), commitment="committed"),
            row(1, "bid_notice", date(2024, 3, 1)),
        ]
    )
    assert "council_mention:committed" not in result["conversion_by_first_signal"]
    assert result["conversion_by_first_signal"]["council_mention:reviewing"]["rate"] == 1.0


async def test_publication_date_not_meeting_date_measures_head_start() -> None:
    result = await measure(
        [
            row(1, "council_mention", date(2024, 1, 1), published=date(2024, 2, 1)),
            row(1, "bid_notice", date(2024, 3, 1)),
        ]
    )
    assert result["lead_time_days"]["median"] == 29


async def test_late_published_minutes_are_not_an_early_public_signal() -> None:
    result = await measure(
        [
            row(1, "council_mention", date(2024, 1, 1), published=date(2024, 4, 1)),
            row(1, "bid_notice", date(2024, 3, 1)),
        ]
    )
    assert result["tender_early_coverage"]["with_early_signal"] == 0


async def test_inferred_publication_date_is_excluded_from_claimed_lead_time() -> None:
    result = await measure(
        [
            row(1, "budget_line", date(2024, 1, 1), provenance="fiscal_year"),
            row(1, "bid_notice", date(2024, 3, 1)),
        ]
    )
    assert result["tender_early_coverage"]["with_early_signal"] == 0
    assert result["excluded_uncertain_dates"] == 1


async def test_current_opportunity_summary_cannot_invent_a_bid() -> None:
    result = await measure([row(1, "council_mention", date(2024, 1, 1))])
    assert result["tender_early_coverage"]["tenders"] == 0
