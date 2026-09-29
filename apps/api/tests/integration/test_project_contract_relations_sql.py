"""Versioned relation decisions against PostgreSQL, including concurrent reviewers."""

import asyncio
import hashlib
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import DBAPIError

from app.api.app import create_app
from app.api.deps import Principal, current_principal, get_session
from app.db.models import (
    Document,
    InstitutionRow,
    Opportunity,
    OpportunityRelationEvent,
    OpportunitySignal,
    Organization,
    Signal,
    Source,
    User,
)
from app.db.session import get_sessionmaker
from app.pipeline.process import ReprocessingProtectedError, protect_human_decisions
from app.pipeline.relations import (
    RelationConflictError,
    RelationValidationError,
    decide_relation,
    has_relation_review_for_document,
    list_relations,
    mixed_group_audit,
    relation_views,
)
from app.settings import get_settings

DAY = date(2026, 9, 29)


async def add_evidence(session, source, opportunity, stage):
    key = uuid4().hex
    text = f"{opportunity.title} 원문 근거 {stage}"
    doc = Document(
        source_id=source.id,
        external_id=key,
        doc_type={"budget_line": "budget_book", "council_mention": "council_minutes"}.get(
            stage, stage
        ),
        title=text,
        text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        mime="text/plain",
        parse_status="parsed",
        published_at=DAY,
        institution_code=opportunity.institution_code,
    )
    session.add(doc)
    await session.flush()
    signal = Signal(
        document_id=doc.id,
        stage=stage,
        institution_code=opportunity.institution_code,
        title=opportunity.title,
        category="facility",
        observed_at=DAY,
        verdict="accepted",
        extractor="test",
        dedupe_key=key,
        evidence=[{"quote": text, "found": True, "start": 0, "end": len(text)}],
    )
    session.add(signal)
    await session.flush()
    session.add(
        OpportunitySignal(
            opportunity_id=opportunity.id,
            signal_id=signal.id,
            score=1,
            method="seed",
            tentative=False,
        )
    )
    await session.flush()
    return signal, doc


async def world(session):
    key = uuid4().hex
    institution = InstitutionRow(
        code=f"REL-{key[:20]}",
        name="관계 검토 기관",
        kind="local_gov",
        sido="서울",
        region_code="11",
    )
    source = Source(key=f"rel-{key}", name="relation test", adapter="fixture", enabled=False)
    organization = Organization(name=f"review-{key}")
    session.add_all([institution, source, organization])
    await session.flush()
    actor = User(
        org_id=organization.id,
        email=f"{key}@example.test",
        name="검토자",
        password_hash="unused",
        is_staff=True,
    )
    session.add(actor)
    opportunities, signals, documents = [], [], []
    for i, stage in enumerate(["budget_line", "bid_notice", "order_plan", "budget_line"]):
        opp = Opportunity(
            institution_code=institution.code,
            title=f"공원 사업 {i}",
            category="facility",
            stage=stage,
            status="open",
            first_seen_at=DAY,
            last_signal_at=DAY,
            signal_count=1,
            est_budget_krw=100_000_000 + i,
            conversion_prob=0.7,
        )
        session.add(opp)
        await session.flush()
        sig, doc = await add_evidence(session, source, opp, stage)
        opportunities.append(opp)
        signals.append(sig)
        documents.append(doc)
    await session.flush()
    return SimpleNamespace(
        session=session,
        source=source,
        actor=actor,
        organization=organization,
        opportunities=opportunities,
        signals=signals,
        documents=documents,
    )


@pytest.fixture
async def relation_world(migrated_db):
    async with get_sessionmaker()() as session:
        yield await world(session)
        await session.rollback()


def request(
    data,
    *,
    contract=1,
    project=0,
    status="confirmed",
    expected_version=0,
    key=None,
    note="원문과 사업 범위를 확인함",
):
    return dict(
        project_id=data.opportunities[project].id,
        contract_id=data.opportunities[contract].id,
        evidence_signal_ids=[data.signals[project].id, data.signals[contract].id],
        status=status,
        expected_version=expected_version,
        note=note,
        actor=data.actor,
        idempotency_key=key or uuid4().hex,
    )


async def test_one_project_has_two_contracts_without_merging_or_summing(relation_world):
    data = relation_world
    before = [
        (opp.id, opp.est_budget_krw, opp.conversion_prob, opp.signal_count)
        for opp in data.opportunities
    ]
    first = await decide_relation(data.session, **request(data, contract=1))
    second = await decide_relation(data.session, **request(data, contract=2))
    assert first.project_id == second.project_id and first.contract_id != second.contract_id
    result = await list_relations(data.session, opportunity_id=first.project_id)
    assert len(result["items"]) == 2
    assert all(item["effective_status"] == "confirmed" for item in result["items"])
    assert before == [
        (opp.id, opp.est_budget_krw, opp.conversion_prob, opp.signal_count)
        for opp in data.opportunities
    ]
    assert (
        await data.session.scalar(
            select(func.count())
            .select_from(OpportunitySignal)
            .where(OpportunitySignal.opportunity_id == first.project_id)
        )
        == 1
    )


async def test_retries_stale_writes_and_redecisions_preserve_history(relation_world):
    data = relation_world
    original = request(data, key="first-decision-001")
    relation = await decide_relation(data.session, **original)
    assert (await decide_relation(data.session, **original)).version == 1
    with pytest.raises(RelationConflictError, match="idempotency_key_reused"):
        await decide_relation(data.session, **(original | {"note": "다른 판정"}))
    with pytest.raises(RelationConflictError, match="stale_version"):
        await decide_relation(
            data.session, **(original | {"idempotency_key": "stale-decision-002"})
        )
    revoked = request(data, status="rejected", expected_version=1)
    revoked["evidence_signal_ids"] = []
    await decide_relation(data.session, **revoked)
    assert (await decide_relation(data.session, **original)).status == "rejected"
    await decide_relation(data.session, **request(data, expected_version=2))
    view = (await relation_views(data.session, [relation], include_history=True))[0]
    assert view["version"] == 3
    assert [event["status"] for event in view["history"]] == ["confirmed", "rejected", "confirmed"]
    assert [event["version"] for event in view["history"]] == [1, 2, 3]


async def test_rejected_deleted_or_changed_sources_make_confirmation_stale(relation_world):
    data = relation_world
    relation = await decide_relation(data.session, **request(data))
    old_evidence = relation.evidence_snapshot
    data.signals[0].verdict = "rejected"
    await data.session.flush()
    view = (await relation_views(data.session, [relation]))[0]
    assert view["effective_status"] == "stale" and view["status"] == "confirmed"
    data.signals[0].verdict = "accepted"
    data.documents[0].text += "\n원문 수정 내용"
    await data.session.flush()
    view = (await relation_views(data.session, [relation]))[0]
    assert view["effective_status"] == "stale" and "evidence_changed" in view["validity_reasons"]
    doc_id, signal_id = data.documents[0].id, data.signals[0].id
    await data.session.execute(delete(Signal).where(Signal.id == signal_id))
    view = (await relation_views(data.session, [relation], include_history=True))[0]
    assert view["effective_status"] == "stale" and view["version"] == 1
    assert view["history"][0]["evidence"] == old_evidence
    assert await has_relation_review_for_document(data.session, doc_id)
    # Revocation still works after evidence is gone.
    await decide_relation(data.session, **request(data, status="rejected", expected_version=1))
    assert relation.status == "rejected"


async def test_a_contract_has_at_most_one_confirmed_project(relation_world):
    data = relation_world
    await decide_relation(data.session, **request(data))
    proposal = await decide_relation(data.session, **request(data, project=3, status="proposed"))
    with pytest.raises(RelationConflictError, match="contract_already_has_confirmed_project"):
        await decide_relation(
            data.session, **request(data, project=3, expected_version=proposal.version)
        )


async def test_cycles_are_rejected_and_mixed_groups_are_only_audited(relation_world):
    data = relation_world
    project, contract = data.opportunities[:2]
    contract_early, _ = await add_evidence(data.session, data.source, contract, "budget_line")
    project_late, _ = await add_evidence(data.session, data.source, project, "order_plan")
    await decide_relation(data.session, **request(data))
    reverse = request(data)
    reverse.update(
        project_id=contract.id,
        contract_id=project.id,
        evidence_signal_ids=[contract_early.id, project_late.id],
    )
    with pytest.raises(RelationValidationError, match="relation_cycle"):
        await decide_relation(data.session, **reverse)
    before = list(
        (
            await data.session.scalars(
                select(OpportunitySignal.signal_id).where(
                    OpportunitySignal.opportunity_id.in_([project.id, contract.id])
                )
            )
        ).all()
    )
    audit = await mixed_group_audit(data.session)
    audited = {item["opportunity"]["id"] for item in audit["items"]}
    assert {project.id, contract.id} <= audited
    after = list(
        (
            await data.session.scalars(
                select(OpportunitySignal.signal_id).where(
                    OpportunitySignal.opportunity_id.in_([project.id, contract.id])
                )
            )
        ).all()
    )
    assert before == after


async def test_concurrent_reviewers_cannot_overwrite_the_same_version(migrated_db):
    async with get_sessionmaker()() as setup:
        data = await world(setup)
        relation = await decide_relation(setup, **request(data))
        relation_id = relation.id
        await setup.commit()

    async def review(note):
        async with get_sessionmaker()() as session:
            try:
                await decide_relation(
                    session, **request(data, status="rejected", expected_version=1, note=note)
                )
                await session.commit()
                return "applied"
            except RelationConflictError as exc:
                await session.rollback()
                return str(exc)

    assert sorted(await asyncio.gather(review("검토자 A의 기각"), review("검토자 B의 기각"))) == [
        "applied",
        "stale_version",
    ]
    async with get_sessionmaker()() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OpportunityRelationEvent)
                .where(OpportunityRelationEvent.relation_id == relation_id)
            )
            == 2
        )


async def test_review_locks_sources_until_history_protects_reprocessing(migrated_db):
    async with get_sessionmaker()() as setup:
        data = await world(setup)
        document_id, signal_id = data.documents[0].id, data.signals[0].id
        await setup.commit()

    async with get_sessionmaker()() as review, get_sessionmaker()() as reprocess:
        await decide_relation(review, **request(data))
        # Ingestion takes Document first. Acquiring it here also proves the reviewer
        # does not take a reverse-order document lock while holding its signal locks.
        await reprocess.execute(
            select(Document.id).where(Document.id == document_id).with_for_update()
        )
        with pytest.raises(DBAPIError) as blocked:
            async with reprocess.begin_nested():
                await reprocess.execute(
                    select(Signal.id).where(Signal.id == signal_id).with_for_update(nowait=True)
                )
        assert blocked.value.orig.sqlstate == "55P03"  # lock_not_available, not a timing guess
        await review.commit()
        with pytest.raises(ReprocessingProtectedError):
            await protect_human_decisions(reprocess, document_id)
        assert await reprocess.get(Signal, signal_id) is not None
        assert await has_relation_review_for_document(reprocess, document_id)
        await reprocess.rollback()


async def test_api_requires_staff_to_write_but_signed_in_users_can_read(relation_world):
    data = relation_world
    relation = await decide_relation(data.session, **request(data))
    application = create_app(get_settings())

    async def session_override():
        yield data.session

    application.dependency_overrides[get_session] = session_override
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        assert (
            await client.get(f"/api/opportunities/{relation.project_id}/relations")
        ).status_code == 401
        member = User(id=999999, is_staff=False, org_id=data.organization.id)
        application.dependency_overrides[current_principal] = lambda: Principal(
            member, data.organization
        )
        public = await client.get(f"/api/opportunities/{relation.project_id}/relations")
        assert (
            public.status_code == 200
            and public.json()["items"][0]["effective_status"] == "confirmed"
        )
        assert public.json()["items"][0]["history"] == []
        assert public.json()["items"][0]["note"] == ""
        assert public.json()["items"][0]["updated_by"] is None
        assert (await client.get("/api/admin/relations")).status_code == 403
        body = {
            key: value
            for key, value in request(data).items()
            if key not in ("actor", "idempotency_key")
        }
        assert (
            await client.post(
                "/api/admin/relations", json=body, headers={"Idempotency-Key": "forbidden-write"}
            )
        ).status_code == 403
        application.dependency_overrides[current_principal] = lambda: Principal(
            data.actor, data.organization
        )
        detail = await client.get(f"/api/admin/relations/{relation.id}")
        assert detail.status_code == 200 and len(detail.json()["history"]) == 1
        assert detail.json()["note"] == relation.note
        assert detail.json()["updated_by"] == data.actor.id
        admin_list = await client.get("/api/admin/relations")
        own = next(item for item in admin_list.json()["items"] if item["id"] == relation.id)
        assert own["note"] == relation.note


async def test_council_evidence_uses_executive_owner_and_rechecks_changed_owner(relation_world):
    data = relation_world
    council = InstitutionRow(
        code=f"CN-{uuid4().hex[:20]}",
        name="사업 소관 의회",
        kind="council",
        sido="서울",
        region_code="11",
        executive_code=data.opportunities[0].institution_code,
    )
    data.session.add(council)
    await data.session.flush()
    data.documents[0].institution_code = council.code
    data.documents[0].doc_type = "council_minutes"
    data.signals[0].stage = "council_mention"
    await data.session.flush()
    relation = await decide_relation(data.session, **request(data))
    assert (await relation_views(data.session, [relation]))[0]["effective_status"] == "confirmed"
    original_history = relation.evidence_snapshot
    council.executive_code = "DIFFERENT-OWNER"
    await data.session.flush()
    view = (await relation_views(data.session, [relation], include_history=True))[0]
    assert view["effective_status"] == "stale"
    assert f"source_institution_mismatch:{data.signals[0].id}" in view["validity_reasons"]
    assert view["history"][0]["evidence"] == original_history
    with pytest.raises(RelationValidationError, match="source_institution_mismatch"):
        await decide_relation(data.session, **request(data, contract=2))
