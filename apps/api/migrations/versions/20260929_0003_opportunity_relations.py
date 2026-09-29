"""Reviewed project-contract edges and durable decision history; no legacy relinking.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "opportunity_relations",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("opportunities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "contract_id",
            sa.BigInteger(),
            sa.ForeignKey("opportunities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), server_default="project_contract", nullable=False),
        sa.Column("status", sa.String(16), server_default="proposed", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "evidence_signal_ids",
            postgresql.ARRAY(sa.BigInteger()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("evidence_snapshot", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column("updated_by", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("kind", "project_id", "contract_id", name="uq_relation_pair"),
        sa.CheckConstraint("kind IN ('project_contract')", name="ck_relation_kind"),
        sa.CheckConstraint(
            "status IN ('proposed', 'confirmed', 'rejected')", name="ck_relation_status"
        ),
        sa.CheckConstraint("project_id <> contract_id", name="ck_relation_not_self"),
        sa.CheckConstraint("version >= 1", name="ck_relation_version"),
    )
    op.create_index(
        "ix_relations_project_status", "opportunity_relations", ["project_id", "status"]
    )
    op.create_index(
        "ix_relations_contract_status", "opportunity_relations", ["contract_id", "status"]
    )
    op.create_index("ix_relations_status_id", "opportunity_relations", ["status", "id"])
    op.create_index(
        "uq_relation_confirmed_contract",
        "opportunity_relations",
        ["contract_id"],
        unique=True,
        postgresql_where=sa.text("status = 'confirmed'"),
    )
    op.create_table(
        "opportunity_relation_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "relation_id",
            sa.BigInteger(),
            sa.ForeignKey("opportunity_relations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(128), unique=True, nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("actor_snapshot", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("evidence_signal_ids", postgresql.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("evidence_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("relation_id", "version", name="uq_relation_event_version"),
        sa.CheckConstraint("version >= 1", name="ck_relation_event_version"),
        sa.CheckConstraint(
            "status IN ('proposed', 'confirmed', 'rejected')", name="ck_relation_event_status"
        ),
    )
    op.create_index(
        "ix_relation_events_evidence",
        "opportunity_relation_events",
        ["evidence_snapshot"],
        postgresql_using="gin",
        postgresql_ops={"evidence_snapshot": "jsonb_path_ops"},
    )


def downgrade() -> None:
    # Dropping reviewed data would violate the history-preservation contract.
    raise RuntimeError("0003 contains review history; an explicit export/migration is required")
