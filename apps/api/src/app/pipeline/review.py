"""Reconcile human decisions in the caller's transaction, without sending notifications."""

from __future__ import annotations

import hashlib
import json
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import today_kst
from app.db.models import Opportunity, OpportunitySignal, Signal
from app.pipeline.link import link_signals, refresh_opportunity
from app.pipeline.revalidate import refresh_affected_recommendations
from app.runtime import Runtime


async def reconcile_reviewed_signal(
    session: AsyncSession,
    runtime: Runtime,
    signal: Signal,
    *,
    today: date | None = None,
) -> list[int]:
    """Repair old and new summaries; keep empty identities and customer history.

    The caller holds the Signal row lock and commits the decision together with this work.
    Repeated delivery of a completed decision retains its resulting link identity.
    """
    today = today or today_kst()
    await session.flush()
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                field: getattr(signal, field)
                for field in (
                    "title",
                    "institution_code",
                    "budget_krw",
                    "expected_year",
                    "expected_half",
                    "commitment",
                    "verdict",
                    "stage",
                )
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    link = await session.scalar(
        select(OpportunitySignal).where(OpportunitySignal.signal_id == signal.id).with_for_update()
    )
    touched: set[int] = set()
    preserve = False
    if link is not None:
        old = await session.get(Opportunity, link.opportunity_id, with_for_update=True)
        assert old is not None
        touched.add(old.id)
        others = await session.scalar(
            select(func.count())
            .select_from(OpportunitySignal)
            .join(Signal, Signal.id == OpportunitySignal.signal_id)
            .where(
                OpportunitySignal.opportunity_id == old.id,
                Signal.id != signal.id,
                Signal.verdict == "accepted",
                OpportunitySignal.tentative.is_(False),
            )
        )
        preserve = (
            signal.verdict == "accepted"
            and old.institution_code == signal.institution_code
            and (
                link.method == "manual"
                or not others
                or link.reasons.get("review_signal_fingerprint") == fingerprint
            )
        )
        if preserve:
            link.tentative = False
            link.reasons = link.reasons | {"review_signal_fingerprint": fingerprint}
        else:
            await session.delete(link)
        await session.flush()
        await refresh_opportunity(session, old, today=today)
        await session.flush()
    if signal.verdict == "accepted" and not preserve:
        touched.update(await link_signals(session, runtime, [signal.id], today=today))
        new_link = await session.scalar(
            select(OpportunitySignal).where(OpportunitySignal.signal_id == signal.id)
        )
        if new_link is not None:
            new_link.reasons = new_link.reasons | {"review_signal_fingerprint": fingerprint}
    await session.flush()
    await refresh_affected_recommendations(session, sorted(touched), today=today)
    return sorted(touched)
