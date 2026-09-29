"""Backtest: were the early signals right, and how early were they?

For every opportunity whose *first* signal was pre-procurement (의회 발언 or 예산 편성) and is old
enough to have had a chance to mature (``horizon_days``), did a tender (입찰공고) follow, and how
many days ahead of it did we know? Conversely, of all tenders in covered institutions, what
share had any earlier public signal?

The same numbers calibrate the ranker: conversion rate by (stage, commitment) replaces the
priors in ``domain/stages.py`` once there are enough samples.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import today_kst
from app.db.models import Document, EvalRun, Opportunity, OpportunitySignal, Signal
from app.domain.stages import CANCELS_KEY, PRE_PROCUREMENT, Stage

METHOD_VERSION = "public-date-cohort-v2"
UNCERTAIN_DATE_SOURCES = frozenset({"fiscal_year", "last_modified", "crawled", "meeting_date"})


async def run_backtest(
    session: AsyncSession,
    *,
    today: date | None = None,
    horizon_days: int = 540,
    min_samples: int = 5,
) -> dict[str, Any]:
    today = today or today_kst()
    if horizon_days <= 0 or min_samples <= 0:
        raise ValueError("horizon_days and min_samples must be positive")
    rows = (
        await session.execute(
            select(
                Opportunity.id.label("opportunity_id"),
                Signal.id,
                Signal.stage,
                Signal.observed_at,
                Signal.commitment,
                Signal.external_refs,
                Document.published_at,
                Document.structured,
            )
            .join(OpportunitySignal, OpportunitySignal.opportunity_id == Opportunity.id)
            .join(Signal, Signal.id == OpportunitySignal.signal_id)
            .join(Document, Document.id == Signal.document_id)
            .where(Signal.verdict == "accepted", OpportunitySignal.tentative.is_(False))
            .order_by(Opportunity.id, Document.published_at, Signal.id)
        )
    ).all()
    by_opp: dict[int, list[tuple[Row[Any], date]]] = defaultdict(list)
    excluded_uncertain_dates = 0
    for sig in rows:
        # Meeting/adoption dates are event dates, not proof of public availability.
        provenance = sig.structured.get("published_from")
        if provenance in UNCERTAIN_DATE_SOURCES or (
            "council_id" in sig.structured and provenance is None
        ):
            excluded_uncertain_dates += 1
            continue
        available = max(sig.published_at, sig.observed_at)
        if available <= today:
            by_opp[sig.opportunity_id].append((sig, available))

    cohorts: dict[str, list[int]] = defaultdict(list)  # key -> [converted 0/1]
    lead_days: list[int] = []
    lead_by_first_stage: dict[str, list[int]] = defaultdict(list)
    tenders_total = 0
    tenders_with_early = 0
    censored = 0
    for records in by_opp.values():
        records.sort(key=lambda r: (r[1], r[0].id))
        first, first_at = records[0]
        # Never use Opportunity.bid_published_at: that summary can contain future data.
        bids = [
            at
            for sig, at in records
            if sig.stage == Stage.BID.value and CANCELS_KEY not in sig.external_refs
        ]
        bid_at = min(bids) if bids else None
        if bid_at is not None:
            tenders_total += 1
            early = [
                (sig, at)
                for sig, at in records
                if Stage(sig.stage) in PRE_PROCUREMENT and at < bid_at
            ]
            if early:
                tenders_with_early += 1
                lead = (bid_at - early[0][1]).days
                lead_days.append(lead)
                lead_by_first_stage[early[0][0].stage].append(lead)
        if Stage(first.stage) not in PRE_PROCUREMENT:
            continue
        horizon_end = first_at + timedelta(days=horizon_days)
        if horizon_end > today:
            censored += 1
            continue  # apply the SAME follow-up requirement to successes and non-successes
        commitment = first.commitment if first.stage == Stage.COUNCIL.value else "committed"
        converted = int(bid_at is not None and first_at < bid_at <= horizon_end)
        cohorts[f"{first.stage}:{commitment or 'none'}"].append(converted)
        cohorts[f"{first.stage}:*"].append(converted)

    conversion = {
        k: {"n": len(v), "rate": round(sum(v) / len(v), 3)} for k, v in sorted(cohorts.items()) if v
    }
    calibration = {
        k: v["rate"]
        for k, v in conversion.items()
        if v["n"] >= min_samples and not k.endswith(":*")
    }
    metrics = {
        "as_of": today.isoformat(),
        "method_version": METHOD_VERSION,
        "evaluation_scope": "retrospective_linked_signals",
        "limitations": [
            "Current link groups are used; this is not a historical prediction replay.",
            "Missing tenders can reflect incomplete source coverage, not non-procurement.",
        ],
        "horizon_days": horizon_days,
        "censored_opportunities": censored,
        "excluded_uncertain_dates": excluded_uncertain_dates,
        "conversion_by_first_signal": conversion,
        "calibration": calibration,
        "lead_time_days": {
            "n": len(lead_days),
            "median": statistics.median(lead_days) if lead_days else None,
            "p25": statistics.quantiles(lead_days, n=4)[0] if len(lead_days) >= 4 else None,
            "p75": statistics.quantiles(lead_days, n=4)[2] if len(lead_days) >= 4 else None,
            "by_first_stage": {
                k: {"n": len(v), "median": statistics.median(v)}
                for k, v in lead_by_first_stage.items()
            },
        },
        "tender_early_coverage": {
            "tenders": tenders_total,
            "with_early_signal": tenders_with_early,
            "rate": round(tenders_with_early / tenders_total, 3) if tenders_total else None,
        },
    }
    return metrics


async def latest_calibration(session: AsyncSession) -> dict[str, float] | None:
    run = await session.scalar(
        select(EvalRun)
        .where(EvalRun.kind == "backtest")
        .order_by(EvalRun.created_at.desc())
        .limit(1)
    )
    if run is None:
        return None
    if run.metrics.get("method_version") != METHOD_VERSION:
        return None  # old, potentially biased results must not silently calibrate new rankings
    cal = run.metrics.get("calibration")
    return {str(k): float(v) for k, v in cal.items()} if isinstance(cal, dict) else None
