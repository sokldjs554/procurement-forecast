"""SQL-backed convergence: arrival order changes history, never final automatic membership."""

from copy import deepcopy
from datetime import UTC, date, datetime
from itertools import permutations
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AlertChannel,
    Brief,
    Document,
    DocumentChunk,
    InstitutionRow,
    LinkReconciliationEvent,
    LinkReconciliationState,
    Notification,
    Opportunity,
    OpportunityRelation,
    OpportunityRelationEvent,
    OpportunitySignal,
    Organization,
    Recommendation,
    ReviewItem,
    Signal,
    Source,
)
from app.db.session import get_sessionmaker
from app.pipeline.link import link_signals, refresh_opportunity
from app.pipeline.link_reconcile import (
    LinkSnapshotChangedError,
    mark_link_dirty,
    reconcile_institution,
)

CODE = "LG-RECONCILE-SQL"
TODAY = date(2026, 9, 29)
OLD_CREATED = datetime(2025, 1, 1, tzinfo=UTC)
LABELS = ("a_x", "b_y", "b_x")
TITLE = "청사 냉난방기 교체"
SUMMARY_FIELDS = (
    "institution_code",
    "department",
    "title",
    "category",
    "stage",
    "status",
    "first_seen_at",
    "last_signal_at",
    "bid_window_start",
    "bid_window_end",
    "bid_published_at",
    "est_budget_krw",
    "best_commitment",
    "signal_count",
    "conversion_prob",
    "keywords",
    "embedding",
)


async def base(session: AsyncSession) -> Source:
    session.add(
        InstitutionRow(
            code=CODE, name="재연결 검증시", kind="local_gov", sido="경기도", region_code="41130"
        )
    )
    source = Source(
        key="reconcile-sql-source", name="SQL convergence test", adapter="fixture", enabled=False
    )
    session.add(source)
    await session.flush()
    return source


async def triple(
    session: AsyncSession, source: Source, insertion_order: tuple[str, ...] = LABELS
) -> dict[str, Signal]:
    documents, chunks = {}, {}
    for book in ("a", "b"):
        doc = Document(
            source_id=source.id,
            external_id=f"book-{book}",
            doc_type="budget_book",
            title=f"예산서 {book}",
            institution_code=CODE,
            published_at=TODAY,
            content_hash=book * 64,
            mime="text/plain",
            text=f"{TITLE} 100,000",
            parse_status="parsed",
            structured={"fiscal_year": 2026},
        )
        session.add(doc)
        await session.flush()
        documents[book] = doc
        for seq, dept in enumerate(("건축과", "회계과")):
            chunk = DocumentChunk(
                document_id=doc.id,
                seq=seq,
                char_start=0,
                char_end=len(doc.text),
                text=doc.text,
                labels=[f"부서: {dept}"],
            )
            session.add(chunk)
            chunks[book, dept] = chunk
    await session.flush()
    rows = {}
    for label in insertion_order:
        book = label[0]
        dept = "회계과" if label == "b_y" else "건축과"
        signal = Signal(
            document_id=documents[book].id,
            chunk_id=chunks[book, dept].id,
            institution_code=CODE,
            department=dept,
            title=TITLE,
            summary="청사 시설개선",
            stage="budget_line",
            category="facility",
            observed_at=TODAY,
            verdict="accepted",
            budget_krw=100_000_000,
            expected_year=2027,
            expected_half="H1",
            commitment="committed",
            confidence=0.99,
            keywords=["청사", "냉난방기"],
            embedding=[1.0] + [0.0] * 511,
            external_refs={},
            evidence=[{"quote": TITLE, "start": 0, "end": len(TITLE), "found": True}],
            grounding={"issues": []},
            extractor="test-budget-v1",
            dedupe_key=f"sql-reconcile-{label}",
        )
        session.add(signal)
        await session.flush()
        rows[label] = signal
    return rows


async def opportunity(session: AsyncSession, title: str = TITLE) -> Opportunity:
    opp = Opportunity(
        institution_code=CODE,
        title=title,
        category="facility",
        stage="budget_line",
        status="open",
        first_seen_at=TODAY,
        last_signal_at=TODAY,
        est_budget_krw=100_000_000,
        conversion_prob=0.55,
        signal_count=0,
    )
    session.add(opp)
    await session.flush()
    return opp


async def attach(session: AsyncSession, opp: Opportunity, signals: list[Signal]) -> None:
    for signal in signals:
        session.add(
            OpportunitySignal(
                opportunity_id=opp.id,
                signal_id=signal.id,
                score=0.91,
                method="seed",
                tentative=False,
                reasons={"previous": "keep in event"},
                created_at=OLD_CREATED,
            )
        )
    await session.flush()
    await refresh_opportunity(session, opp, today=TODAY)
    await session.flush()


def plain(value: Any) -> Any:
    return value.tolist() if hasattr(value, "tolist") else deepcopy(value)


async def signature(
    session: AsyncSession, signals: dict[str, Signal]
) -> dict[tuple[str, ...], dict[str, Any]]:
    names = {signal.id: name for name, signal in signals.items()}
    links = (
        await session.execute(
            select(OpportunitySignal.signal_id, OpportunitySignal.opportunity_id).where(
                OpportunitySignal.signal_id.in_(names)
            )
        )
    ).all()
    by_opp: dict[int, list[str]] = {}
    for signal_id, opp_id in links:
        by_opp.setdefault(opp_id, []).append(names[signal_id])
    result = {}
    for opp_id, labels in by_opp.items():
        opp = await session.get(Opportunity, opp_id, populate_existing=True)
        assert opp is not None
        result[tuple(sorted(labels))] = {
            field: plain(getattr(opp, field)) for field in SUMMARY_FIELDS
        }
    return result


async def memberships(session: AsyncSession, rows: dict[str, Signal]) -> dict[int, int]:
    return dict(
        (
            await session.execute(
                select(OpportunitySignal.signal_id, OpportunitySignal.opportunity_id).where(
                    OpportunitySignal.signal_id.in_([s.id for s in rows.values()])
                )
            )
        ).all()
    )


async def event_count(session: AsyncSession) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(LinkReconciliationEvent)
            .where(LinkReconciliationEvent.institution_code == CODE)
        )
        or 0
    )


async def test_all_insertion_and_arrival_orders_converge_full_membership_and_summary(runtime):  # type: ignore[no-untyped-def]
    patterns = ((3,), (1, 2), (2, 1), (1, 1, 1))
    expected = None
    async with get_sessionmaker()() as session:
        source = await base(session)
        for insert_index, insertion_order in enumerate(permutations(LABELS)):
            for arrival_index, arrival_order in enumerate(permutations(LABELS)):
                case = await session.begin_nested()
                rows = await triple(session, source, insertion_order)
                at = 0
                for size in patterns[(insert_index * 6 + arrival_index) % len(patterns)]:
                    batch = [rows[name].id for name in arrival_order[at : at + size]]
                    await link_signals(session, runtime, batch, today=TODAY)
                    result = await reconcile_institution(session, runtime, CODE, today=TODAY)
                    assert result.processed
                    at += size
                actual = await signature(session, rows)
                assert sorted(map(len, actual)) == [1, 2]
                assert not any({"b_x", "b_y"} <= set(group) for group in actual)
                if expected is None:
                    expected = actual
                assert actual == expected, (
                    insertion_order,
                    arrival_order,
                    patterns[(insert_index * 6 + arrival_index) % 4],
                )
                before = await memberships(session, rows)
                count = await event_count(session)
                again = await reconcile_institution(session, runtime, CODE, today=TODAY)
                assert not again.processed and again.changed_signals == 0
                assert await memberships(session, rows) == before
                assert await event_count(session) == count
                await case.rollback()
        await session.rollback()


async def test_dirty_generation_identity_matching_and_event_history_are_idempotent(runtime):  # type: ignore[no-untyped-def]
    async with get_sessionmaker()() as session:
        rows = await triple(session, await base(session))
        original = await opportunity(session)
        original_id = original.id
        await attach(session, original, list(rows.values()))
        await mark_link_dirty(session, {CODE})
        first = await reconcile_institution(session, runtime, CODE, today=TODAY)
        assert first.processed and first.generation == 1 and first.changed_signals == 1
        after = await memberships(session, rows)
        assert sum(opp_id == original_id for opp_id in after.values()) == 2
        assert len(set(after.values())) == 2
        events = list(
            await session.scalars(
                select(LinkReconciliationEvent).where(
                    LinkReconciliationEvent.institution_code == CODE
                )
            )
        )
        assert len(events) == 3
        assert all(event.before["opportunity_id"] == original_id for event in events)
        assert all(event.before["reasons"] == {"previous": "keep in event"} for event in events)
        assert all(event.after["opportunity_id"] == after[event.signal_id] for event in events)
        assert all(len(event.stable_key) == len(event.input_digest) == 64 for event in events)
        links = list(
            await session.scalars(
                select(OpportunitySignal).where(OpportunitySignal.signal_id.in_(after))
            )
        )
        assert all(link.created_at == OLD_CREATED for link in links)
        assert await session.get(Opportunity, original_id) is not None
        assert (
            await session.scalar(
                select(func.count()).select_from(Signal).where(Signal.id.in_(after))
            )
            == 3
        )
        await mark_link_dirty(session, {CODE})
        second = await reconcile_institution(session, runtime, CODE, today=TODAY)
        assert second.processed and second.generation == 2 and second.changed_signals == 0
        assert await memberships(session, rows) == after
        assert await event_count(session) == 3
        state = await session.get(LinkReconciliationState, CODE, populate_existing=True)
        assert state.generation == state.reconciled_generation == 2
        assert state.recommendations_generation == 0
        await session.rollback()


async def test_unlinked_later_rows_do_not_influence_current_reconciliation(runtime):  # type: ignore[no-untyped-def]
    async with get_sessionmaker()() as session:
        rows = await triple(session, await base(session))
        await link_signals(session, runtime, [rows["a_x"].id], today=TODAY)
        await reconcile_institution(session, runtime, CODE, today=TODAY)
        assert set(await signature(session, rows)) == {("a_x",)}
        await link_signals(session, runtime, [rows["b_y"].id], today=TODAY)
        await reconcile_institution(session, runtime, CODE, today=TODAY)
        assert set(await signature(session, rows)) == {("a_x", "b_y")}
        assert (
            await session.scalar(
                select(OpportunitySignal).where(OpportunitySignal.signal_id == rows["b_x"].id)
            )
            is None
        )
        await link_signals(session, runtime, [rows["b_x"].id], today=TODAY)
        state = await session.get(LinkReconciliationState, CODE, populate_existing=True)
        assert state.generation > state.reconciled_generation
        await reconcile_institution(session, runtime, CODE, today=TODAY)
        assert sorted(map(len, await signature(session, rows))) == [1, 2]
        await session.rollback()


async def install_history_protection(
    session: AsyncSession,
    target: Opportunity,
    document_id: int,
    kind: str,
) -> Any:
    """Create historical anchors without a current review/membership of target's signals."""
    if kind == "notification_history":
        org = Organization(name="Historical notification recipient")
        session.add(org)
        await session.flush()
        channel = AlertChannel(
            org_id=org.id, kind="email", target="history@example.test", enabled=False
        )
        session.add(channel)
        await session.flush()
        row = Notification(
            org_id=org.id,
            channel_id=channel.id,
            kind="digest",
            status="sent",
            dedupe_key="reconcile-notification-only",
            payload={"items": [{"opportunity_id": target.id}]},
            sent_at=OLD_CREATED,
        )
        session.add(row)
        await session.flush()
        assert (
            await session.scalar(
                select(Recommendation).where(Recommendation.opportunity_id == target.id)
            )
            is None
        )
        return row
    archived = Signal(
        document_id=document_id,
        institution_code=CODE,
        stage="budget_line",
        title="과거 검토 근거",
        summary="이력",
        category="facility",
        observed_at=TODAY,
        confidence=0.9,
        evidence=[],
        grounding={},
        external_refs={},
        keywords=[],
        commitment="planned",
        verdict="accepted" if kind == "relation_event_old_opportunity" else "rejected",
        extractor="test-history",
        dedupe_key="sql-reconcile-history-signal",
    )
    session.add(archived)
    await session.flush()
    if kind in ("review_previous_link", "review_history"):
        resolution = {"action": "reject"}
        previous = {"opportunity_id": target.id, "method": "similarity", "score": 0.9}
        if kind == "review_previous_link":
            resolution["previous_link"] = previous
        else:
            resolution["history"] = [{"action": "approve", "previous_link": previous}]
        review = ReviewItem(
            signal_id=archived.id,
            status="rejected",
            reasons=[],
            resolution=resolution,
            resolved_at=OLD_CREATED,
        )
        session.add(review)
        await session.flush()
        assert (
            await session.scalar(
                select(OpportunitySignal).where(OpportunitySignal.signal_id == archived.id)
            )
            is None
        )
        return review
    assert kind == "relation_event_old_opportunity"
    moved = await opportunity(session, "Moved historical evidence")
    await attach(session, moved, [archived])
    old_project = await opportunity(session, "Historical relation project")
    old_contract = await opportunity(session, "Historical relation contract")
    relation = OpportunityRelation(
        project_id=old_project.id,
        contract_id=old_contract.id,
        kind="project_contract",
        status="rejected",
        version=1,
        evidence_signal_ids=[],
        evidence_snapshot=[],
        note="old relation",
    )
    session.add(relation)
    await session.flush()
    event = OpportunityRelationEvent(
        relation_id=relation.id,
        version=1,
        status="rejected",
        idempotency_key="old-opportunity-evidence-event",
        request_digest="b" * 64,
        actor_snapshot={},
        evidence_signal_ids=[archived.id],
        evidence_snapshot=[{"signal_id": archived.id, "opportunity_id": target.id}],
        note="Signal moved; its original published identity still has history",
    )
    session.add(event)
    await session.flush()
    assert moved.id != target.id
    return event


@pytest.mark.parametrize(
    "protection",
    [
        "manual",
        "review",
        "relation",
        "relation_event",
        "feedback",
        "notified",
        "brief",
        "notification_history",
        "review_previous_link",
        "review_history",
        "relation_event_old_opportunity",
    ],
)
async def test_human_and_published_identities_are_protected_as_whole_opportunities(
    runtime, protection: str
):  # type: ignore[no-untyped-def]
    async with get_sessionmaker()() as session:
        rows = await triple(session, await base(session))
        target = await opportunity(session)
        target_id = target.id
        await attach(session, target, list(rows.values()))
        before = await memberships(session, rows)
        before_summary = await signature(session, rows)
        signal_id = rows["a_x"].id
        protected_row = None
        if protection == "manual":
            link = await session.scalar(
                select(OpportunitySignal).where(OpportunitySignal.signal_id == signal_id)
            )
            link.method = "manual"
        elif protection == "review":
            protected_row = ReviewItem(
                signal_id=signal_id,
                status="approved",
                reasons=["human"],
                resolution={"action": "approve", "changes": {}},
                resolved_at=OLD_CREATED,
            )
            session.add(protected_row)
        elif protection in ("relation", "relation_event"):
            contract = await opportunity(session, "별도 계약")
            project = (
                target if protection == "relation" else await opportunity(session, "과거 사업")
            )
            relation = OpportunityRelation(
                project_id=project.id,
                contract_id=contract.id,
                status="rejected",
                kind="project_contract",
                version=1,
                evidence_signal_ids=[],
                evidence_snapshot=[],
                note="keep relationship history",
            )
            session.add(relation)
            await session.flush()
            protected_row = relation
            if protection == "relation_event":
                protected_row = OpportunityRelationEvent(
                    relation_id=relation.id,
                    version=1,
                    status="rejected",
                    idempotency_key="reconcile-protection-event",
                    request_digest="a" * 64,
                    actor_snapshot={},
                    evidence_signal_ids=[signal_id],
                    evidence_snapshot=[{"signal_id": signal_id}],
                    note="keep old evidence",
                )
                session.add(protected_row)
        elif protection in (
            "notification_history",
            "review_previous_link",
            "review_history",
            "relation_event_old_opportunity",
        ):
            protected_row = await install_history_protection(
                session, target, rows["a_x"].document_id, protection
            )
        else:
            org = Organization(name="Reconciliation customer")
            session.add(org)
            await session.flush()
            if protection == "brief":
                protected_row = Brief(
                    org_id=org.id,
                    opportunity_id=target.id,
                    content_md="Keep this paid report",
                    model="test",
                    credits_spent=1,
                    idempotency_key="reconcile-paid-brief",
                )
            else:
                protected_row = Recommendation(
                    org_id=org.id,
                    opportunity_id=target.id,
                    score=0.91,
                    breakdown={"human": True},
                    ranker_version="old",
                    feedback="relevant" if protection == "feedback" else None,
                    notified_stage="budget_line" if protection == "notified" else None,
                )
            session.add(protected_row)
        await session.flush()
        protected_snapshot = (
            {
                column.key: plain(getattr(protected_row, column.key))
                for column in protected_row.__table__.columns
            }
            if protected_row is not None
            else None
        )
        await mark_link_dirty(session, {CODE})
        result = await reconcile_institution(session, runtime, CODE, today=TODAY)
        assert result.processed and result.protected_opportunities >= 1
        assert result.changed_signals == 0
        assert await memberships(session, rows) == before
        assert await signature(session, rows) == before_summary
        assert await session.get(Opportunity, target_id) is not None
        assert await event_count(session) == 0
        if protected_row is not None:
            await session.refresh(protected_row)
            assert {
                column.key: plain(getattr(protected_row, column.key))
                for column in protected_row.__table__.columns
            } == protected_snapshot
        if protection == "manual":
            assert (
                await session.scalar(
                    select(OpportunitySignal).where(OpportunitySignal.signal_id == signal_id)
                )
            ).method == "manual"
        await session.rollback()


async def test_snapshot_conflict_does_not_partially_apply_memberships_or_events(
    runtime, monkeypatch
):  # type: ignore[no-untyped-def]
    from app.pipeline import link_reconcile

    async with get_sessionmaker()() as session:
        rows = await triple(session, await base(session))
        original_opp = await opportunity(session)
        await attach(session, original_opp, list(rows.values()))
        await mark_link_dirty(session, {CODE})
        before = await memberships(session, rows)
        document_id = rows["a_x"].document_id
        original_load = link_reconcile._load_scope
        calls = 0

        async def changed_after_first_read(current_session, institution_code, generation):  # type: ignore[no-untyped-def]
            nonlocal calls
            scope = await original_load(current_session, institution_code, generation)
            calls += 1
            if calls == 1:
                # Model an upstream source change after planning's first snapshot, before
                # the application savepoint/locks. Real SQL changes the stable source key.
                await current_session.execute(
                    update(Document)
                    .where(Document.id == document_id)
                    .values(external_id="book-a-revised")
                )
            return scope

        monkeypatch.setattr(link_reconcile, "_load_scope", changed_after_first_read)
        with pytest.raises(LinkSnapshotChangedError, match="changed"):
            await reconcile_institution(session, runtime, CODE, today=TODAY)
        assert calls == 2
        assert await memberships(session, rows) == before
        assert await event_count(session) == 0
        state = await session.get(LinkReconciliationState, CODE, populate_existing=True)
        assert state.generation == 1 and state.reconciled_generation == 0
        assert (
            await session.scalar(select(Document.external_id).where(Document.id == document_id))
            == "book-a-revised"
        )
        await session.rollback()


@pytest.mark.parametrize("mode", ["similarity", "ref"])
@pytest.mark.parametrize("protection", ["manual", "notification_history", "review_history"])
async def test_provisional_arrivals_cannot_attach_to_protected_identities(
    runtime, mode: str, protection: str
):  # type: ignore[no-untyped-def]
    async with get_sessionmaker()() as session:
        rows = await triple(session, await base(session))
        [target_id] = await link_signals(session, runtime, [rows["a_x"].id], today=TODAY)
        target = await session.get(Opportunity, target_id)
        if mode == "ref":
            rows["a_x"].external_refs = {"order_plan_no": "PROTECTED-PLAN"}
            rows["b_x"].external_refs = {"order_plan_no": "PROTECTED-PLAN"}
        if protection == "manual":
            link = await session.scalar(
                select(OpportunitySignal).where(OpportunitySignal.signal_id == rows["a_x"].id)
            )
            link.method = "manual"
        else:
            await install_history_protection(session, target, rows["a_x"].document_id, protection)
        await session.flush()
        before = await signature(session, {"a_x": rows["a_x"]})
        await link_signals(session, runtime, [rows["b_x"].id], today=TODAY)
        after = await memberships(session, rows)
        assert after[rows["a_x"].id] == target_id
        assert after[rows["b_x"].id] != target_id
        assert await signature(session, {"a_x": rows["a_x"]}) == before
        assert (
            await session.scalar(
                select(OpportunitySignal).where(OpportunitySignal.signal_id == rows["b_y"].id)
            )
            is None
        )
        await session.rollback()
