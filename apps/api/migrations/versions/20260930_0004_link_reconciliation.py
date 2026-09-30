"""Durable automatic-link convergence and append-only membership history.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_recommendations_opportunity", "recommendations", ["opportunity_id"])
    op.create_index("ix_briefs_opportunity", "briefs", ["opportunity_id"])
    op.create_table(
        "opportunity_customer_anchors",
        sa.Column(
            "opportunity_id",
            sa.BigInteger(),
            sa.ForeignKey("opportunities.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("signal_ids", postgresql.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_customer_anchor_signals",
        "opportunity_customer_anchors",
        ["signal_ids"],
        postgresql_using="gin",
    )
    op.create_table(
        "link_reconciliation_states",
        sa.Column(
            "institution_code", sa.String(32), sa.ForeignKey("institutions.code"), primary_key=True
        ),
        sa.Column("generation", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("reconciled_generation", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "recommendations_generation", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.CheckConstraint(
            "generation >= reconciled_generation AND reconciled_generation >= "
            "recommendations_generation AND recommendations_generation >= 0",
            name="ck_link_reconciliation_generations",
        ),
    )
    # Existing automatic data also converges after upgrade; the worker sweep is restart-safe.
    op.execute("""
        INSERT INTO link_reconciliation_states (institution_code)
        SELECT DISTINCT o.institution_code FROM opportunities o
        JOIN opportunity_signals os ON os.opportunity_id = o.id
        WHERE o.institution_code IS NOT NULL
    """)
    op.create_table(
        "link_reconciliation_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "institution_code", sa.String(32), sa.ForeignKey("institutions.code"), nullable=False
        ),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("signal_id", sa.BigInteger(), nullable=False),
        sa.Column("stable_key", sa.Text(), nullable=False),
        sa.Column("before", postgresql.JSONB(), nullable=False),
        sa.Column("after", postgresql.JSONB(), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "institution_code", "generation", "signal_id", name="uq_link_reconcile_event"
        ),
    )
    op.create_index("ix_link_reconcile_events_signal", "link_reconciliation_events", ["signal_id"])
    op.create_index(
        "ix_relation_events_signal_ids",
        "opportunity_relation_events",
        ["evidence_signal_ids"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_review_resolution",
        "review_items",
        ["resolution"],
        postgresql_using="gin",
        postgresql_ops={"resolution": "jsonb_path_ops"},
    )
    op.create_index(
        "ix_notification_payload",
        "notifications",
        ["payload"],
        postgresql_using="gin",
        postgresql_ops={"payload": "jsonb_path_ops"},
    )


def downgrade() -> None:
    raise RuntimeError(
        "0004 contains membership history; export/migrate explicitly before removing"
    )
