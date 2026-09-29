"""Versioned project-contract relation API; kept separate from the core feed schema."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

RelationStatus = Literal["proposed", "confirmed", "rejected"]


class RelationDecisionIn(BaseModel):
    project_id: int = Field(gt=0)
    contract_id: int = Field(gt=0)
    status: RelationStatus
    expected_version: int = Field(
        ge=0, description="0 for creation; otherwise the version shown to the reviewer"
    )
    evidence_signal_ids: list[int] = Field(default_factory=list, max_length=20)
    note: str = Field(min_length=1, max_length=2000)


class RelationQuoteOut(BaseModel):
    quote: str
    source_quote: str
    start: int
    end: int


class RelationEvidenceOut(BaseModel):
    signal_id: int
    signal_title: str
    stage: str
    opportunity_id: int
    document_id: int
    document_title: str
    document_url: str | None
    document_content_hash: str
    source_text_sha256: str
    fingerprint: str
    evidence: list[RelationQuoteOut]


class RelationEndpointOut(BaseModel):
    id: int
    title: str
    institution_code: str | None
    stage: str


class RelationEventOut(BaseModel):
    version: int
    status: RelationStatus
    note: str
    actor_user_id: int | None
    actor_name: str | None
    created_at: datetime
    evidence_signal_ids: list[int]
    evidence: list[RelationEvidenceOut]


class RelationOut(BaseModel):
    id: int
    kind: Literal["project_contract"]
    project_id: int
    contract_id: int
    status: RelationStatus
    effective_status: Literal["proposed", "confirmed", "rejected", "stale"]
    valid: bool
    validity_reasons: list[str]
    version: int
    evidence_signal_ids: list[int]
    evidence: list[RelationEvidenceOut]
    note: str
    updated_by: int | None
    created_at: datetime
    updated_at: datetime
    project: RelationEndpointOut | None
    contract: RelationEndpointOut | None
    history: list[RelationEventOut]


class RelationPageOut(BaseModel):
    items: list[RelationOut]
    next_after_id: int | None


class MixedRelationGroupOut(BaseModel):
    opportunity: RelationEndpointOut
    project_signal_ids: list[int]
    contract_signal_ids: list[int]
    bid_notice_numbers: list[str]
    reason: str


class MixedRelationPageOut(BaseModel):
    items: list[MixedRelationGroupOut]
    next_after_id: int | None
