"""Reference identity and retained history must survive conservative relinking."""

import re
from dataclasses import FrozenInstanceError
from datetime import date
from difflib import SequenceMatcher
from itertools import product
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.models import Opportunity, Signal
from app.domain.synonyms import canonical_terms, canonicalize
from app.domain.text import char_ngrams, jaccard
from app.pipeline import link
from app.pipeline.link import _without_conflicting_numbers, decide, refresh_opportunity
from app.pipeline.process import canonical_title

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


async def test_surviving_candidate_uses_its_stored_vector_after_structural_filters(monkeypatch):
    opp = opportunity()
    opp.title = "본관 기계설비 개선"
    opp.est_budget_krw = None
    opp.embedding = None  # metadata retrieval has not loaded the persisted vector
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [(opp.id, [1.0] + [0.0] * 511)]))
    )
    signal = Signal(
        id=2,
        institution_code="TEST",
        external_refs={},
        title="청사 냉난방기 교체",
        category="facility",
        stage="order_plan",
        budget_krw=None,
        observed_at=TODAY,
        embedding=[1.0] + [0.0] * 511,
    )
    monkeypatch.setattr(link, "_reference_match", AsyncMock(return_value=None))
    monkeypatch.setattr(link, "_candidates", AsyncMock(return_value=[opp]))
    monkeypatch.setattr(link, "_without_conflicting_numbers", AsyncMock(return_value=[opp]))
    monkeypatch.setattr(link, "_without_other_budget_rows", AsyncMock(return_value=[opp]))
    runtime = SimpleNamespace(settings=SimpleNamespace(link_threshold=0.6, link_review_band=0.08))
    result = await decide(session, runtime, signal)
    assert result is not None and result.opportunity_id == opp.id


def _uncached_title_results(a, b):
    """Pre-cache scoring equations, kept independent of the new fingerprint helper."""
    ca, cb = canonicalize(canonical_title(a)), canonicalize(canonical_title(b))
    ngrams = max(
        jaccard(set(char_ngrams(ca, 2)), set(char_ngrams(cb, 2))),
        jaccard(set(char_ngrams(ca, 3)), set(char_ngrams(cb, 3))),
    )
    ta, tb = canonical_terms(a), canonical_terms(b)
    shared = len(ta & tb) / max(min(len(ta), len(tb)), 1) if ta and tb else 0.0
    similarity = max(ngrams, 0.8 * shared)
    x, y = (re.sub(r"[\s()\[\]{}·ㆍ,.\-_/]", "", t) for t in (ca, cb))
    agree = x == y
    if not agree and similarity >= 0.5:
        edits = {tag for tag, *_ in SequenceMatcher(None, x, y, autojunk=False).get_opcodes()}
        agree = edits in ({"equal", "insert"}, {"equal", "delete"})
    return similarity, agree, bool(ta) and bool(tb) and not (ta & tb)


def test_cached_title_results_preserve_uncached_equations_for_cold_and_warm_inputs():
    titles = (
        "",
        "스마트 쉘터",
        "[긴급] 2026년 스마트 버스정류장 조성 (재공고)",
        "수정청소년수련관 시설개선",
        "중원청소년수련관 시설 개선",
        "판교도서관 냉난방기 교체",
        "성남시판교도서관 냉난방기 교체",
        "AI 청사 안내",
        "[CCTV] 2026년 공원 정비",
        "공원 정비",
        "ＡＩ\u3000누리집\u200b 정비",
        "스마트쉘터" * 60,
    )
    pairs = list(product(titles, repeat=2))
    expected = [_uncached_title_results(a, b) for a, b in pairs]
    link._cached_title_fingerprint.cache_clear()
    for _ in range(2):
        actual = [
            (link.title_similarity(a, b), link.budget_names_agree(a, b), link.terms_conflict(a, b))
            for a, b in pairs
        ]
        assert actual == expected


def test_reused_title_fingerprint_cannot_be_mutated():
    first = link._title_fingerprint("[CCTV] 공원 정비")
    assert first is link._title_fingerprint("[CCTV] 공원 정비")
    with pytest.raises(FrozenInstanceError):
        first.budget_name = "다른 사업"
    with pytest.raises(AttributeError):
        first.terms.add("스마트폴")


def test_extreme_titles_are_computed_without_cache_retention():
    title = "스마트쉘터" * 60
    first = link._title_fingerprint(title)
    second = link._title_fingerprint(title)
    assert first == second and first is not second
