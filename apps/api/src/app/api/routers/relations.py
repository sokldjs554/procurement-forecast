"""Staff-reviewed relationships and read-only opportunity relation evidence."""

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query

from app.api.deps import PrincipalDep, SessionDep, StaffDep
from app.api.relation_schemas import (
    MixedRelationPageOut,
    RelationDecisionIn,
    RelationOut,
    RelationPageOut,
    RelationStatus,
)
from app.db.models import Opportunity, OpportunityRelation
from app.pipeline.relations import (
    RelationConflictError,
    RelationValidationError,
    decide_relation,
    list_relations,
    mixed_group_audit,
    relation_views,
)

router = APIRouter(tags=["relations"])


@router.get("/api/admin/relations", response_model=RelationPageOut)
async def admin_relations(
    _: StaffDep,
    session: SessionDep,
    status: RelationStatus | None = None,
    after_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> RelationPageOut:
    return RelationPageOut.model_validate(
        await list_relations(
            session,
            status=status,
            after_id=after_id,
            limit=limit,
            include_private=True,
        )
    )


@router.get("/api/admin/relations/mixed", response_model=MixedRelationPageOut)
async def mixed_relations(
    _: StaffDep,
    session: SessionDep,
    after_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MixedRelationPageOut:
    return MixedRelationPageOut.model_validate(
        await mixed_group_audit(session, after_id=after_id, limit=limit)
    )


@router.get("/api/admin/relations/{relation_id}", response_model=RelationOut)
async def admin_relation(relation_id: int, _: StaffDep, session: SessionDep) -> RelationOut:
    relation = await session.get(OpportunityRelation, relation_id)
    if relation is None:
        raise HTTPException(404, "relation_not_found")
    return RelationOut.model_validate(
        (await relation_views(session, [relation], include_history=True))[0]
    )


@router.post("/api/admin/relations", response_model=RelationOut)
async def review_relation(
    body: RelationDecisionIn,
    principal: StaffDep,
    session: SessionDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> RelationOut:
    try:
        relation = await decide_relation(
            session,
            **body.model_dump(),
            actor=principal.user,
            idempotency_key=idempotency_key,
        )
    except RelationValidationError as exc:
        raise HTTPException(422, {"reason": "invalid_relation", "issues": exc.reasons}) from exc
    except RelationConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, "staff_required") from exc
    return RelationOut.model_validate(
        (await relation_views(session, [relation], include_history=True))[0]
    )


@router.get("/api/opportunities/{opportunity_id}/relations", response_model=RelationPageOut)
async def opportunity_relations(
    opportunity_id: int,
    _: PrincipalDep,
    session: SessionDep,
    after_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> RelationPageOut:
    if await session.get(Opportunity, opportunity_id) is None:
        raise HTTPException(404, "opportunity_not_found")
    return RelationPageOut.model_validate(
        await list_relations(
            session,
            opportunity_id=opportunity_id,
            after_id=after_id,
            limit=limit,
        )
    )
