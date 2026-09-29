"""Ambiguous evidence must not silently merge unrelated purchases."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.models import Opportunity, Signal
from app.pipeline import link


def opportunity(id, title="스마트쉘터 설치"):
    return Opportunity(
        id=id,
        title=title,
        category="smart_city",
        est_budget_krw=300_000_000,
        stage="budget_line",
        first_seen_at=date(2026, 1, 1),
        last_signal_at=date(2026, 1, 1),
        embedding=None,
    )


async def choose(monkeypatch, scores, title="스마트쉘터 설치", titles=None):
    signal = Signal(
        title=title,
        category="smart_city",
        budget_krw=300_000_000,
        observed_at=date(2026, 2, 1),
        stage="council_mention",
        institution_code="X",
    )
    candidates = [opportunity(i + 1, titles[i] if titles else title) for i in range(len(scores))]
    monkeypatch.setattr(link, "_reference_match", AsyncMock(return_value=None))
    monkeypatch.setattr(link, "_candidates", AsyncMock(return_value=candidates))
    monkeypatch.setattr(link, "_without_conflicting_numbers", AsyncMock(return_value=candidates))
    monkeypatch.setattr(link, "_without_other_budget_rows", AsyncMock(return_value=candidates))
    monkeypatch.setattr(
        link,
        "score_candidate",
        lambda s, o: (scores[o.id - 1], {"title": link.title_similarity(s.title, o.title)}),
    )
    runtime = SimpleNamespace(settings=SimpleNamespace(link_threshold=0.6, link_review_band=0.08))
    return await link.decide(None, runtime, signal)


async def test_same_amount_and_category_do_not_supply_project_identity(monkeypatch):
    assert (
        await choose(monkeypatch, [0.45], title="청사 안내판 교체", titles=["공원 분수 보수"])
        is None
    )


async def test_review_band_does_not_merge_opportunity(monkeypatch):
    assert await choose(monkeypatch, [0.55, 0.2]) is None


async def test_close_candidates_abstain(monkeypatch):
    assert await choose(monkeypatch, [0.8, 0.79]) is None


async def test_clear_winner_still_links(monkeypatch):
    result = await choose(monkeypatch, [0.8, 0.5])
    assert result is not None and result.opportunity_id == 1 and not result.tentative


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("수정청소년수련관 시설개선", "중원청소년수련관 시설개선"),
        ("판교도서관 냉난방기 교체", "분당도서관 냉난방기 교체"),
    ],
)
def test_different_named_facilities_cannot_merge(a, b):
    signal = Signal(
        title=a,
        category="smart_city",
        budget_krw=300_000_000,
        observed_at=date(2026, 2, 1),
        stage="council_mention",
        embedding=None,
    )
    score, reasons = link.score_candidate(signal, opportunity(1, b))
    assert score == 0
    assert reasons["facility_conflict"] == 1.0
