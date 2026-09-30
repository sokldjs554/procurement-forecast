"""A protected identity must not accept a late provisional automatic attachment."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.db.models import Opportunity, OpportunitySignal, Signal
from app.pipeline import link, link_reconcile, link_state

TODAY = date(2026, 9, 29)


class Session:
    def __init__(self, *, protected=False, disappeared=False, count=1):
        self.rows = [
            Signal(
                id=number,
                institution_code="A",
                title="청사 냉난방기 교체",
                stage="order_plan",
                category="facility",
                observed_at=TODAY,
                verdict="accepted",
                keywords=[],
            )
            for number in range(1, count + 1)
        ]
        self.target = None if disappeared else Opportunity(id=20, institution_code="A")
        self.protected = protected
        self.calls = []
        self.added = []
        self.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: self.rows))
        self.flush = AsyncMock()

    async def get(self, model, key, **kwargs):
        self.calls.append(("get", key, kwargs))
        return self.target

    async def scalar(self, query):
        self.calls.append(("guard", query))
        return 20 if self.protected else None

    def add(self, row):
        self.calls.append(("add", type(row).__name__))
        if isinstance(row, Opportunity):
            row.id = 100 + len(self.added)
        self.added.append(row)


def setup(monkeypatch, session):
    async def anchor(locked_session, opportunity_id):
        assert locked_session is session
        session.calls.append(("anchor", opportunity_id))

    decision = AsyncMock(return_value=link.LinkDecision(20, 1.0, "ref", False, {}))
    monkeypatch.setattr(link, "decide", decision)
    monkeypatch.setattr(link, "refresh_opportunity", AsyncMock())
    monkeypatch.setattr(link_state, "protected_opportunity_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(
        link_state, "ensure_customer_anchor", AsyncMock(side_effect=anchor), raising=False
    )
    monkeypatch.setattr(link_reconcile, "mark_link_dirty", AsyncMock())
    return decision


async def test_protection_committed_after_initial_scan_seeds_without_touching_target(monkeypatch):
    session = Session(protected=True)
    decide = setup(monkeypatch, session)
    touched = await link.link_signals(session, SimpleNamespace(), [1], today=TODAY)
    membership = next(row for row in session.added if isinstance(row, OpportunitySignal))
    assert membership.opportunity_id != 20 and membership.method == "seed"
    assert 20 not in touched
    assert session.calls[0] == ("get", 20, {"with_for_update": True, "populate_existing": True})
    assert session.calls[1][0] == "guard"
    assert decide.await_count == 1  # protected reference never falls back to a different target
    link_state.ensure_customer_anchor.assert_not_awaited()
    link_state.protected_opportunity_ids.assert_awaited_once_with(
        session, {"A"}, include_customer=False
    )


async def test_guard_is_after_target_lock_and_before_first_attachment(monkeypatch):
    session = Session(count=2)
    setup(monkeypatch, session)
    assert await link.link_signals(session, SimpleNamespace(), [1, 2], today=TODAY) == [20]
    memberships = [row for row in session.added if isinstance(row, OpportunitySignal)]
    assert len(memberships) == 2 and all(row.opportunity_id == 20 for row in memberships)
    guard_indices = [i for i, call in enumerate(session.calls) if call[0] == "guard"]
    assert len(guard_indices) == 1  # the chosen target stays locked through the batch
    assert session.calls[0] == ("get", 20, {"with_for_update": True, "populate_existing": True})
    assert session.calls[1][0] == "guard"
    assert session.calls[2] == ("anchor", 20) and session.calls[3] == ("add", "OpportunitySignal")
    link_state.ensure_customer_anchor.assert_awaited_once_with(session, 20)


async def test_target_deleted_while_waiting_for_lock_becomes_a_seed(monkeypatch):
    session = Session(disappeared=True)
    setup(monkeypatch, session)
    touched = await link.link_signals(session, SimpleNamespace(), [1], today=TODAY)
    assert len(touched) == 1 and touched[0] != 20
    assert not any(call[0] == "guard" for call in session.calls)
    assert next(row for row in session.added if isinstance(row, OpportunitySignal)).method == "seed"
