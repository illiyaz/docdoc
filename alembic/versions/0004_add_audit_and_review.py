"""Add Phase 4 audit_events and review_tasks tables

Replaces Phase 1 audit_events / review_tasks / review_decisions with
Phase 4 schemas for HITL workflow and append-only audit trail.

Revision ID: 0004_add_audit_and_review
Revises: 0003_add_notification_lists
Create Date: 2026-02-23

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0004_add_audit_and_review"
down_revision: str | None = "0003_add_notification_lists"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- Drop old Phase 1 tables (order matters: review_decisions → review_tasks) --
    op.drop_table("review_decisions")
    op.drop_table("review_tasks")
    op.drop_table("audit_events")

    # -- Phase 4 audit_events --------------------------------------------------
    op.create_table(
        "audit_events",
        sa.Column("audit_event_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "actor",
            sa.String(length=128),
            server_default=sa.text("'system'"),
            nullable=False,
        ),
        sa.Column("subject_id", sa.String(length=128), nullable=True),
        sa.Column("pii_record_id", sa.String(length=128), nullable=True),
        sa.Column("decision", sa.String(length=32), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("regulatory_basis", sa.Text(), nullable=True),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "immutable",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("audit_event_id"),
    )
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])
    op.create_index("ix_audit_events_subject_id", "audit_events", ["subject_id"])

    # -- Phase 4 review_tasks --------------------------------------------------
    op.create_table(
        "review_tasks",
        sa.Column("review_task_id", sa.Uuid(), nullable=False),
        sa.Column("queue_type", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("assigned_to", sa.String(length=128), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("required_role", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("review_task_id"),
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["notification_subjects.subject_id"],
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_review_tasks_queue_type", "review_tasks", ["queue_type"])
    op.create_index("ix_review_tasks_status", "review_tasks", ["status"])

    # -- Recreate review_decisions with updated FK ----------------------------
    op.create_table(
        "review_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("review_task_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reviewer_id", sa.String(length=128), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("corrected_fields", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["review_task_id"],
            ["review_tasks.review_task_id"],
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("review_decisions")
    op.drop_table("review_tasks")
    op.drop_table("audit_events")
