"""Exercise the actual SQL filters and publication-date join in PostgreSQL."""

from datetime import date

from sqlalchemy import delete, select

from app.db.models import Document, Opportunity, OpportunitySignal, Signal
from app.db.session import get_sessionmaker
from app.pipeline.backtest import run_backtest


async def test_only_accepted_confirmed_public_signals_count(demo_world):
    # All edits stay in this transaction, rolled back when the session closes.
    async with get_sessionmaker()() as s:
        council = await s.scalar(select(Signal).where(Signal.stage == "council_mention").limit(1))
        bid = await s.scalar(select(Signal).where(Signal.stage == "bid_notice").limit(1))
        opp = await s.scalar(select(Opportunity).limit(1))
        assert council and bid and opp
        await s.execute(delete(OpportunitySignal))
        for sig, observed, published in [
            (council, date(2024, 1, 1), date(2024, 2, 1)),
            (bid, date(2024, 3, 1), date(2024, 3, 1)),
        ]:
            sig.observed_at, sig.verdict, sig.external_refs = observed, "accepted", {}
            sig.commitment = "planned"
            doc = await s.get(Document, sig.document_id)
            assert doc
            doc.published_at, doc.structured = published, {}
            s.add(
                OpportunitySignal(
                    opportunity_id=opp.id,
                    signal_id=sig.id,
                    method="similarity",
                    score=1,
                    tentative=False,
                    reasons={},
                )
            )
        await s.flush()
        result = await run_backtest(s, today=date(2026, 9, 29))
        assert result["lead_time_days"]["median"] == 29
        assert result["tender_early_coverage"]["tenders"] == 1
        council.verdict = "needs_review"
        await s.flush()
        result = await run_backtest(s, today=date(2026, 9, 29))
        assert result["tender_early_coverage"]["with_early_signal"] == 0
        council.verdict = "accepted"
        edge = await s.get(OpportunitySignal, (opp.id, bid.id))
        assert edge
        edge.tentative = True
        await s.flush()
        result = await run_backtest(s, today=date(2026, 9, 29))
        assert result["tender_early_coverage"]["tenders"] == 0
        assert result["conversion_by_first_signal"]["council_mention:planned"]["rate"] == 0
