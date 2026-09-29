"""Real SQL regressions for reference identity, retrieval recall, and refreshed summaries."""

from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect

from app.db.models import Document, InstitutionRow, Opportunity, OpportunitySignal, Signal, Source
from app.db.session import get_sessionmaker
from app.pipeline.link import _candidates, decide, refresh_opportunity

TODAY = date(2026, 9, 29)
TITLE = "청사 냉난방기 교체"
VECTOR = [1.0] + [0.0] * 511
RUNTIME = SimpleNamespace(settings=SimpleNamespace(link_threshold=0.6, link_review_band=0.08))


@pytest.fixture
async def link_db(migrated_db):
    async with get_sessionmaker()() as session:
        source = Source(key="link-safety", name="test", adapter="fixture")
        session.add(source)
        for code in ("LINK-A", "LINK-B"):
            session.add(
                InstitutionRow(
                    code=code, name=code, kind="local_gov", sido="서울", region_code="11"
                )
            )
        await session.flush()
        yield session, source
        await session.rollback()


async def signal(
    session,
    source,
    key,
    *,
    refs=None,
    institution="LINK-A",
    stage="order_plan",
    verdict="accepted",
    embedding=VECTOR,
):
    doc = Document(
        source_id=source.id,
        external_id=key,
        doc_type=stage if stage != "budget_line" else "budget_book",
        title=TITLE,
        institution_code=institution,
        published_at=TODAY,
        content_hash=key,
        mime="text/plain",
    )
    session.add(doc)
    await session.flush()
    row = Signal(
        document_id=doc.id,
        institution_code=institution,
        stage=stage,
        title=TITLE,
        category="facility",
        budget_krw=100_000_000,
        observed_at=TODAY,
        external_refs=refs or {},
        embedding=embedding,
        verdict=verdict,
        extractor="test",
        dedupe_key=key,
    )
    session.add(row)
    await session.flush()
    return row


async def thread(session, sig, *, tentative=False):
    opp = Opportunity(
        institution_code=sig.institution_code,
        title=TITLE,
        category="facility",
        stage=sig.stage,
        status="open",
        first_seen_at=TODAY,
        last_signal_at=TODAY,
        est_budget_krw=sig.budget_krw,
        signal_count=1,
        embedding=sig.embedding,
        conversion_prob=0.5,
    )
    session.add(opp)
    await session.flush()
    session.add(
        OpportunitySignal(
            opportunity_id=opp.id, signal_id=sig.id, score=1.0, method="seed", tentative=tentative
        )
    )
    await session.flush()
    return opp


async def test_reference_cannot_cross_institutions(link_db):
    session, source = link_db
    old = await signal(session, source, "old", refs={"order_plan_no": "P"}, institution="LINK-B")
    await thread(session, old)
    new = await signal(session, source, "new", refs={"order_plan_no": "P"})
    assert await decide(session, RUNTIME, new) is None


async def test_different_reference_keys_pointing_to_two_threads_abstain(link_db):
    session, source = link_db
    for key, refs in [("plan", {"order_plan_no": "P"}), ("spec", {"prespec_no": "S"})]:
        await thread(session, await signal(session, source, key, refs=refs))
    new = await signal(session, source, "new", refs={"order_plan_no": "P", "prespec_no": "S"})
    assert await decide(session, RUNTIME, new) is None


@pytest.mark.parametrize(
    "refs", [{"bid_notice_no": "B2", "order_plan_no": "P"}, {"bid_notice_nos": ["B1", "B2"]}]
)
async def test_shared_plan_or_many_bids_cannot_merge_distinct_contracts(link_db, refs):
    session, source = link_db
    old = await signal(session, source, "old", refs={"order_plan_no": "P", "bid_notice_no": "B1"})
    await thread(session, old)
    new = await signal(session, source, "new", refs=refs)
    assert await decide(session, RUNTIME, new) is None


@pytest.mark.parametrize(
    "refs",
    [
        {"bid_notice_no": "B1", "bid_notice_ord": "001"},
        {"bid_notice_no": "B1", "cancels_bid_notice_no": "B1"},
        {"bid_notice_nos": ["B1"]},
    ],
)
async def test_same_bid_revisions_cancellation_and_single_target_plan_link(link_db, refs):
    session, source = link_db
    old = await signal(session, source, "old", refs={"bid_notice_no": "B1"})
    opp = await thread(session, old)
    new = await signal(session, source, "new", refs=refs)
    result = await decide(session, RUNTIME, new)
    assert result and result.opportunity_id == opp.id and result.method == "ref"


async def test_cancellation_finds_own_tender_when_other_tender_shares_plan(link_db):
    session, source = link_db
    first = await thread(
        session,
        await signal(session, source, "first", refs={"order_plan_no": "P", "bid_notice_no": "B1"}),
    )
    await thread(
        session,
        await signal(session, source, "second", refs={"order_plan_no": "P", "bid_notice_no": "B2"}),
    )
    cancel = await signal(
        session,
        source,
        "cancel",
        refs={"order_plan_no": "P", "bid_notice_no": "B1", "cancels_bid_notice_no": "B1"},
    )
    result = await decide(session, RUNTIME, cancel)
    assert result and result.opportunity_id == first.id and result.method == "ref"


@pytest.mark.parametrize("embedding", [VECTOR, None])
async def test_valid_candidate_beyond_twelve_conflicting_threads_is_retrieved(link_db, embedding):
    session, source = link_db
    for index in range(12):
        old = await signal(
            session,
            source,
            f"old-{index}",
            refs={"order_plan_no": f"OTHER-{index}"},
            embedding=embedding,
        )
        await thread(session, old)
    eligible = await thread(session, await signal(session, source, "eligible", embedding=embedding))
    new = await signal(session, source, "new", refs={"order_plan_no": "NEW"}, embedding=embedding)
    result = await decide(session, RUNTIME, new)
    assert result and result.opportunity_id == eligible.id


async def test_metadata_filters_defer_vectors_until_survivors_are_scored(link_db):
    session, source = link_db
    valid = await thread(session, await signal(session, source, "valid"))
    valid.title = "본관 기계설비 개선"
    valid.est_budget_krw = None
    valid_id = valid.id
    invalid = await thread(
        session, await signal(session, source, "invalid", refs={"order_plan_no": "OTHER"})
    )
    invalid_id = invalid.id
    await session.flush()
    # Fresh identities model normal retrieval, rather than fixture objects with loaded vectors.
    session.expunge_all()
    source = await session.get(Source, source.id)
    assert source is not None
    incoming = await signal(session, source, "new", refs={"order_plan_no": "NEW"})
    incoming.budget_krw = None
    candidates = await _candidates(session, incoming)
    assert {candidate.id for candidate in candidates} == {valid_id, invalid_id}
    assert all("embedding" in inspect(candidate).unloaded for candidate in candidates)
    result = await decide(session, RUNTIME, incoming)
    assert result and result.opportunity_id == valid_id
    survivor = next(candidate for candidate in candidates if candidate.id == valid_id)
    rejected = next(candidate for candidate in candidates if candidate.id == invalid_id)
    assert "embedding" not in inspect(survivor).unloaded
    assert "embedding" in inspect(rejected).unloaded


@pytest.mark.parametrize(
    ("verdict", "tentative"), [("rejected", False), ("needs_review", False), ("accepted", True)]
)
async def test_ineligible_membership_cannot_supply_reference_or_active_summary(
    link_db, verdict, tentative
):
    session, source = link_db
    old = await signal(session, source, "old", refs={"order_plan_no": "P"}, verdict=verdict)
    opp = await thread(session, old, tentative=tentative)
    new = await signal(session, source, "new", refs={"order_plan_no": "P"})
    assert await decide(session, RUNTIME, new) is None
    await refresh_opportunity(session, opp, today=TODAY)
    await session.flush()
    assert await session.get(Opportunity, opp.id) is opp
    assert opp.status == "dormant" and opp.signal_count == 0
    assert opp.est_budget_krw is None and opp.conversion_prob == 0
    assert await _candidates(session, new) == []


async def test_rejected_bid_does_not_advance_accepted_budget_summary(link_db):
    session, source = link_db
    budget = await signal(session, source, "budget", stage="budget_line", embedding=None)
    opp = await thread(session, budget)
    opp.embedding = VECTOR
    bid = await signal(session, source, "bid", stage="bid_notice", verdict="rejected")
    bid.budget_krw = 999_000_000
    session.add(
        OpportunitySignal(
            opportunity_id=opp.id, signal_id=bid.id, score=1, method="similarity", tentative=False
        )
    )
    await session.flush()
    await refresh_opportunity(session, opp, today=TODAY)
    assert opp.stage == "budget_line" and opp.signal_count == 1
    assert opp.bid_published_at is None and opp.est_budget_krw == 100_000_000
    assert opp.embedding is None
