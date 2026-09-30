"""ORM models.

Conventions
-----------
* Status/kind columns are ``text`` + ``CHECK`` instead of PostgreSQL ENUM types: adding a value is
  a one-line migration instead of an ``ALTER TYPE`` dance that cannot run inside a transaction.
* Every table that receives external data has a natural idempotency key (``(source_id,
  external_id)``, ``dedupe_key``, ``idempotency_key``, ``order_id``) so retries are safe.
* Embeddings are ``vector(512)`` (pgvector) with HNSW cosine indexes, created in the migration.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 512


def _check_in(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)


class Base(DeclarativeBase):
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy API
        dict[str, Any]: JSONB,
        list[dict[str, Any]]: JSONB,
    }


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def _pk() -> Mapped[int]:
    return mapped_column(BigInteger, Identity(always=False), primary_key=True)


# ------------------------------------------------------------------------------------------------
# Tenancy & auth
# ------------------------------------------------------------------------------------------------
PLANS = ("free", "pro", "team")


class Organization(TimestampMixin, Base):
    __tablename__ = "organizations"
    __table_args__ = (_check_in("plan", PLANS, "ck_org_plan"),)

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(200))
    plan: Mapped[str] = mapped_column(String(20), default="free", server_default="free")
    credit_balance: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Monthly ceiling on metered usage (usage_events.usd); None = no ceiling.
    usage_cap_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))

    users: Mapped[list[User]] = relationship(back_populates="organization")
    profile: Mapped[CompanyProfile | None] = relationship(back_populates="organization")


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (_check_in("role", ("owner", "member"), "ck_user_role"),)

    id: Mapped[int] = _pk()
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default="owner", server_default="owner")
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    organization: Mapped[Organization] = relationship(back_populates="users")


class CompanyProfile(Base):
    """What the customer sells — the query side of recommendation."""

    __tablename__ = "company_profiles"

    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    exclude_keywords: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}"
    )
    categories: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    region_codes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    budget_min: Mapped[int | None] = mapped_column(BigInteger)
    budget_max: Mapped[int | None] = mapped_column(BigInteger)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    organization: Mapped[Organization] = relationship(back_populates="profile")


# ------------------------------------------------------------------------------------------------
# Reference data
# ------------------------------------------------------------------------------------------------
class InstitutionRow(Base):
    __tablename__ = "institutions"
    __table_args__ = (
        _check_in(
            "kind",
            ("local_gov", "council", "education_office", "public_agency", "central"),
            "ck_institution_kind",
        ),
    )

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(32))
    sido: Mapped[str] = mapped_column(String(32))
    sigungu: Mapped[str | None] = mapped_column(String(32))
    region_code: Mapped[str] = mapped_column(String(10), index=True)
    executive_code: Mapped[str | None] = mapped_column(String(32))
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")


# ------------------------------------------------------------------------------------------------
# Ingestion
# ------------------------------------------------------------------------------------------------
DOC_TYPES = ("council_minutes", "budget_book", "order_plan", "prespec", "bid_notice", "award")


class Source(TimestampMixin, Base):
    __tablename__ = "sources"

    id: Mapped[int] = _pk()
    key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    adapter: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    config: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    cursor: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class IngestRun(Base):
    __tablename__ = "ingest_runs"
    __table_args__ = (
        _check_in("status", ("running", "succeeded", "partial", "failed"), "ck_ingest_status"),
        Index("ix_ingest_runs_source_started", "source_id", "started_at"),
    )

    id: Mapped[int] = _pk()
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")


class Document(TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_documents_source_external"),
        _check_in("doc_type", DOC_TYPES, "ck_document_type"),
        _check_in(
            "parse_status", ("pending", "parsed", "failed", "skipped"), "ck_document_parse_status"
        ),
        Index("ix_documents_type_published", "doc_type", "published_at"),
        Index("ix_documents_pending", "parse_status", postgresql_where="parse_status = 'pending'"),
    )

    id: Mapped[int] = _pk()
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    external_id: Mapped[str] = mapped_column(String(128))
    doc_type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    publisher_raw: Mapped[str | None] = mapped_column(Text)
    institution_code: Mapped[str | None] = mapped_column(ForeignKey("institutions.code"))
    department: Mapped[str | None] = mapped_column(String(100))
    published_at: Mapped[date] = mapped_column(Date)
    content_hash: Mapped[str] = mapped_column(String(64))
    mime: Mapped[str] = mapped_column(String(100))
    raw_uri: Mapped[str | None] = mapped_column(Text)
    structured: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    text: Mapped[str | None] = mapped_column(Text)
    text_quality: Mapped[float | None] = mapped_column(Float)
    parse_method: Mapped[str | None] = mapped_column(String(32))
    parse_status: Mapped[str] = mapped_column(String(16), default="pending")
    parse_error: Mapped[str | None] = mapped_column(Text)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="DocumentChunk.seq"
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (UniqueConstraint("document_id", "seq", name="uq_chunk_doc_seq"),)

    id: Mapped[int] = _pk()
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    # Speakers for minutes ("위원 박지훈"), section labels for budget books ("부서: 스마트도시과").
    labels: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    triage_score: Mapped[float | None] = mapped_column(Float)
    triage_passed: Mapped[bool | None] = mapped_column(Boolean)

    document: Mapped[Document] = relationship(back_populates="chunks")


# ------------------------------------------------------------------------------------------------
# Signals & opportunities
# ------------------------------------------------------------------------------------------------
STAGES = ("council_mention", "budget_line", "order_plan", "prespec", "bid_notice", "award")
VERDICTS = ("accepted", "needs_review", "rejected")
COMMITMENTS = ("committed", "planned", "reviewing", "declined")


class Signal(TimestampMixin, Base):
    __tablename__ = "signals"
    __table_args__ = (
        _check_in("stage", STAGES, "ck_signal_stage"),
        _check_in("verdict", VERDICTS, "ck_signal_verdict"),
        Index("ix_signals_institution_observed", "institution_code", "observed_at"),
        Index("ix_signals_verdict", "verdict"),
    )

    id: Mapped[int] = _pk()
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    chunk_id: Mapped[int | None] = mapped_column(
        ForeignKey("document_chunks.id", ondelete="SET NULL")
    )
    stage: Mapped[str] = mapped_column(String(32))
    institution_code: Mapped[str | None] = mapped_column(ForeignKey("institutions.code"))
    speaker_institution_code: Mapped[str | None] = mapped_column(ForeignKey("institutions.code"))
    department: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(32))
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    budget_krw: Mapped[int | None] = mapped_column(BigInteger)
    expected_year: Mapped[int | None] = mapped_column(Integer)
    expected_half: Mapped[str | None] = mapped_column(String(2))
    commitment: Mapped[str | None] = mapped_column(String(16))
    procurement_type: Mapped[str | None] = mapped_column(String(16))
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(default=list, server_default="[]")
    grounding: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    verdict: Mapped[str] = mapped_column(String(16), default="accepted")
    extractor: Mapped[str] = mapped_column(String(100))
    observed_at: Mapped[date] = mapped_column(Date)
    external_refs: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)

    document: Mapped[Document] = relationship()
    link: Mapped[OpportunitySignal | None] = relationship(back_populates="signal")


class Opportunity(TimestampMixin, Base):
    __tablename__ = "opportunities"
    __table_args__ = (
        _check_in("stage", STAGES, "ck_opportunity_stage"),
        _check_in("status", ("open", "bid_open", "closed", "dormant"), "ck_opportunity_status"),
        Index("ix_opportunities_status_window", "status", "bid_window_start"),
        Index("ix_opportunities_institution", "institution_code"),
    )

    id: Mapped[int] = _pk()
    institution_code: Mapped[str | None] = mapped_column(ForeignKey("institutions.code"))
    department: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(32))
    stage: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="open")
    first_seen_at: Mapped[date] = mapped_column(Date)
    last_signal_at: Mapped[date] = mapped_column(Date)
    bid_window_start: Mapped[date | None] = mapped_column(Date)
    bid_window_end: Mapped[date | None] = mapped_column(Date)
    bid_published_at: Mapped[date | None] = mapped_column(Date)
    est_budget_krw: Mapped[int | None] = mapped_column(BigInteger)
    best_commitment: Mapped[str | None] = mapped_column(String(16))
    signal_count: Mapped[int] = mapped_column(Integer, default=0)
    conversion_prob: Mapped[float] = mapped_column(Float, default=0.0)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    links: Mapped[list[OpportunitySignal]] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan"
    )


class OpportunitySignal(TimestampMixin, Base):
    __tablename__ = "opportunity_signals"
    __table_args__ = (
        _check_in("method", ("ref", "similarity", "seed", "manual"), "ck_link_method"),
    )

    opportunity_id: Mapped[int] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"), primary_key=True
    )
    signal_id: Mapped[int] = mapped_column(
        ForeignKey("signals.id", ondelete="CASCADE"), primary_key=True, unique=True
    )
    score: Mapped[float] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(16))
    tentative: Mapped[bool] = mapped_column(Boolean, default=False)
    reasons: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")

    opportunity: Mapped[Opportunity] = relationship(back_populates="links")
    signal: Mapped[Signal] = relationship(back_populates="link")


class OpportunityCustomerAnchor(Base):
    """The original evidence behind a customer identity; later automatic evidence can evolve."""

    __tablename__ = "opportunity_customer_anchors"
    __table_args__ = (Index("ix_customer_anchor_signals", "signal_ids", postgresql_using="gin"),)

    opportunity_id: Mapped[int] = mapped_column(
        ForeignKey("opportunities.id", ondelete="RESTRICT"), primary_key=True
    )
    signal_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LinkReconciliationState(Base):
    """Durable convergence and recommendation-dispatch generations, per demand owner."""

    __tablename__ = "link_reconciliation_states"
    __table_args__ = (
        CheckConstraint(
            "generation >= reconciled_generation AND reconciled_generation >= "
            "recommendations_generation AND recommendations_generation >= 0",
            name="ck_link_reconciliation_generations",
        ),
    )

    institution_code: Mapped[str] = mapped_column(ForeignKey("institutions.code"), primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, server_default="1")
    reconciled_generation: Mapped[int] = mapped_column(BigInteger, server_default="0")
    recommendations_generation: Mapped[int] = mapped_column(BigInteger, server_default="0")


class LinkReconciliationEvent(Base):
    """Prior and replacement automatic memberships; survives source re-extraction."""

    __tablename__ = "link_reconciliation_events"
    __table_args__ = (
        UniqueConstraint(
            "institution_code", "generation", "signal_id", name="uq_link_reconcile_event"
        ),
        Index("ix_link_reconcile_events_signal", "signal_id"),
    )

    id: Mapped[int] = _pk()
    institution_code: Mapped[str] = mapped_column(ForeignKey("institutions.code"))
    generation: Mapped[int] = mapped_column(BigInteger)
    # Snapshot identity, deliberately not a cascading signal FK.
    signal_id: Mapped[int] = mapped_column(BigInteger)
    stable_key: Mapped[str] = mapped_column(Text)
    before: Mapped[dict[str, Any]] = mapped_column()
    after: Mapped[dict[str, Any]] = mapped_column()
    input_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OpportunityRelation(TimestampMixin, Base):
    """A reviewed edge; never changes primary signal ownership or monetary aggregates."""

    __tablename__ = "opportunity_relations"
    __table_args__ = (
        UniqueConstraint("kind", "project_id", "contract_id", name="uq_relation_pair"),
        _check_in("kind", ("project_contract",), "ck_relation_kind"),
        _check_in("status", ("proposed", "confirmed", "rejected"), "ck_relation_status"),
        CheckConstraint("project_id <> contract_id", name="ck_relation_not_self"),
        CheckConstraint("version >= 1", name="ck_relation_version"),
        Index("ix_relations_project_status", "project_id", "status"),
        Index("ix_relations_contract_status", "contract_id", "status"),
        Index("ix_relations_status_id", "status", "id"),
        Index(
            "uq_relation_confirmed_contract",
            "contract_id",
            unique=True,
            postgresql_where="status = 'confirmed'",
        ),
    )

    id: Mapped[int] = _pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id", ondelete="RESTRICT"))
    contract_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(
        String(32), default="project_contract", server_default="project_contract"
    )
    status: Mapped[str] = mapped_column(String(16), default="proposed", server_default="proposed")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # IDs are anchors, not cascading FKs. Re-extraction must not destroy a human decision.
    evidence_signal_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), default=list, server_default="{}"
    )
    evidence_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(
        default=list, server_default="[]"
    )
    note: Mapped[str] = mapped_column(Text, default="", server_default="")
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OpportunityRelationEvent(TimestampMixin, Base):
    """Append-only decisions, including their exact evidence and idempotency identity."""

    __tablename__ = "opportunity_relation_events"
    __table_args__ = (
        UniqueConstraint("relation_id", "version", name="uq_relation_event_version"),
        CheckConstraint("version >= 1", name="ck_relation_event_version"),
        _check_in("status", ("proposed", "confirmed", "rejected"), "ck_relation_event_status"),
        Index(
            "ix_relation_events_evidence",
            "evidence_snapshot",
            postgresql_using="gin",
            postgresql_ops={"evidence_snapshot": "jsonb_path_ops"},
        ),
        Index("ix_relation_events_signal_ids", "evidence_signal_ids", postgresql_using="gin"),
    )

    id: Mapped[int] = _pk()
    relation_id: Mapped[int] = mapped_column(
        ForeignKey("opportunity_relations.id", ondelete="RESTRICT")
    )
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_digest: Mapped[str] = mapped_column(String(64))
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor_snapshot: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    evidence_signal_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))
    evidence_snapshot: Mapped[list[dict[str, Any]]] = mapped_column()
    note: Mapped[str] = mapped_column(Text)


class Recommendation(Base):
    __tablename__ = "recommendations"
    __table_args__ = (
        _check_in(
            "feedback",
            ("relevant", "irrelevant", "won", "dismissed"),
            "ck_recommendation_feedback",
        ),
        Index("ix_recommendations_org_score", "org_id", "score"),
        Index("ix_recommendations_opportunity", "opportunity_id"),
    )

    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    opportunity_id: Mapped[int] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"), primary_key=True
    )
    score: Mapped[float] = mapped_column(Float)
    breakdown: Mapped[dict[str, Any]] = mapped_column(default=dict)
    ranker_version: Mapped[str] = mapped_column(String(32))
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    notified_stage: Mapped[str | None] = mapped_column(String(32))
    feedback: Mapped[str | None] = mapped_column(String(16))
    feedback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    opportunity: Mapped[Opportunity] = relationship()


class ReviewItem(TimestampMixin, Base):
    __tablename__ = "review_items"
    __table_args__ = (
        _check_in("status", ("open", "approved", "edited", "rejected"), "ck_review_status"),
        Index(
            "ix_review_resolution",
            "resolution",
            postgresql_using="gin",
            postgresql_ops={"resolution": "jsonb_path_ops"},
        ),
    )

    id: Mapped[int] = _pk()
    signal_id: Mapped[int] = mapped_column(
        ForeignKey("signals.id", ondelete="CASCADE"), unique=True
    )
    reasons: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    status: Mapped[str] = mapped_column(String(16), default="open")
    resolution: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    signal: Mapped[Signal] = relationship()


# ------------------------------------------------------------------------------------------------
# LLM operations
# ------------------------------------------------------------------------------------------------
class LLMCall(TimestampMixin, Base):
    __tablename__ = "llm_calls"
    __table_args__ = (
        _check_in(
            "status",
            ("ok", "error", "refusal", "cache_hit", "budget_skip", "invalid_output"),
            "ck_llm_status",
        ),
        Index("ix_llm_calls_created", "created_at"),
        Index("ix_llm_calls_task_created", "task", "created_at"),
    )

    id: Mapped[int] = _pk()
    task: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal(0))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16))
    served_by: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)


class LLMCacheEntry(TimestampMixin, Base):
    __tablename__ = "llm_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    task: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    response: Mapped[dict[str, Any]] = mapped_column()
    hits: Mapped[int] = mapped_column(Integer, default=0)


# ------------------------------------------------------------------------------------------------
# Alerts
# ------------------------------------------------------------------------------------------------
class AlertChannel(TimestampMixin, Base):
    __tablename__ = "alert_channels"
    __table_args__ = (_check_in("kind", ("email", "slack", "kakao"), "ck_channel_kind"),)

    id: Mapped[int] = _pk()
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    target: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(String(100), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_error: Mapped[str | None] = mapped_column(Text)


class AlertRule(Base):
    __tablename__ = "alert_rules"
    __table_args__ = (_check_in("mode", ("instant", "daily", "weekly"), "ck_alert_mode"),)

    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    mode: Mapped[str] = mapped_column(String(16), default="daily")
    min_score: Mapped[float] = mapped_column(Float, default=0.55)
    stages: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=lambda: list(STAGES), server_default="{}"
    )
    quiet_start: Mapped[int] = mapped_column(Integer, default=22)
    quiet_end: Mapped[int] = mapped_column(Integer, default=8)


class Notification(TimestampMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (
        _check_in("status", ("pending", "sent", "failed", "skipped"), "ck_notification_status"),
        Index("ix_notifications_pending", "status", "scheduled_at"),
        Index(
            "ix_notification_payload",
            "payload",
            postgresql_using="gin",
            postgresql_ops={"payload": "jsonb_path_ops"},
        ),
    )

    id: Mapped[int] = _pk()
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    channel_id: Mapped[int] = mapped_column(ForeignKey("alert_channels.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column()
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ------------------------------------------------------------------------------------------------
# Billing
# ------------------------------------------------------------------------------------------------
class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        _check_in("plan", PLANS, "ck_subscription_plan"),
        _check_in(
            "status", ("trialing", "active", "past_due", "canceled"), "ck_subscription_status"
        ),
        Index("ix_subscriptions_due", "status", "next_charge_at"),
    )

    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    plan: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(16))
    customer_key: Mapped[str] = mapped_column(String(64), unique=True)
    billing_key_enc: Mapped[str | None] = mapped_column(Text)
    card_summary: Mapped[str | None] = mapped_column(String(64))
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_charge_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Payment(TimestampMixin, Base):
    __tablename__ = "payments"
    __table_args__ = (
        _check_in("kind", ("subscription", "credit_pack"), "ck_payment_kind"),
        _check_in("status", ("pending", "paid", "failed", "canceled"), "ck_payment_status"),
    )

    id: Mapped[int] = _pk()
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    order_id: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    order_name: Mapped[str] = mapped_column(String(100))
    amount: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    provider: Mapped[str] = mapped_column(String(16))
    provider_payment_key: Mapped[str | None] = mapped_column(String(200))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    failure_message: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CreditLedgerEntry(TimestampMixin, Base):
    """Append-only. ``organizations.credit_balance`` is a cache of the latest ``balance_after``,
    updated in the same transaction under a row lock."""

    __tablename__ = "credit_ledger"
    __table_args__ = (
        _check_in(
            "reason",
            ("plan_grant", "purchase", "brief", "refund", "adjustment", "expiry"),
            "ck_ledger_reason",
        ),
        CheckConstraint("balance_after >= 0", name="ck_ledger_non_negative"),
        Index("ix_credit_ledger_org_created", "org_id", "created_at"),
    )

    id: Mapped[int] = _pk()
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    delta: Mapped[int] = mapped_column(Integer)
    balance_after: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(16))
    ref_type: Mapped[str | None] = mapped_column(String(32))
    ref_id: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class Brief(TimestampMixin, Base):
    __tablename__ = "briefs"
    __table_args__ = (
        Index("ix_briefs_org_opportunity", "org_id", "opportunity_id"),
        Index("ix_briefs_opportunity", "opportunity_id"),
    )

    id: Mapped[int] = _pk()
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id", ondelete="CASCADE"))
    content_md: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(64))
    credits_spent: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)


# ------------------------------------------------------------------------------------------------
# Operations
# ------------------------------------------------------------------------------------------------
class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        _check_in("status", ("running", "succeeded", "failed", "retrying"), "ck_job_status"),
        Index("ix_job_runs_started", "started_at"),
        Index("ix_job_runs_failed", "status", postgresql_where="status = 'failed'"),
    )

    id: Mapped[int] = _pk()
    job: Mapped[str] = mapped_column(String(64))
    job_id: Mapped[str] = mapped_column(String(128))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16))
    args: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    result: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class EvalRun(TimestampMixin, Base):
    __tablename__ = "eval_runs"
    __table_args__ = (
        _check_in("kind", ("extraction", "backtest", "ocr", "triage", "ranking"), "ck_eval_kind"),
    )

    id: Mapped[int] = _pk()
    kind: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(100))
    metrics: Mapped[dict[str, Any]] = mapped_column()
    params: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    git_sha: Mapped[str | None] = mapped_column(String(40))


# ------------------------------------------------------------------------------------------------
# Job queue and metering (migration 0005). The queue's behaviour is in the jobq_* SQL functions;
# these mappings are for reading and for tests.
# ------------------------------------------------------------------------------------------------
class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = _pk()
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(24), unique=True)
    secret_sha256: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = _pk()
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), server_default="queued")
    priority: Mapped[int] = mapped_column(Integer, server_default="0")
    payload: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="5")
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_by: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, server_default="false")
    progress: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    result: Mapped[dict[str, Any] | None] = mapped_column()
    error: Mapped[str | None] = mapped_column(Text)
    cost: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[int] = _pk()
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    meter: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default="0")
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
