"""Add notification_subjects table

Revision ID: 0002_add_notification_subjects
Revises: 0001_initial
Create Date: 2026-02-23

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0002_add_notification_subjects"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_subjects",
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_name", sa.String(length=512), nullable=True),
        sa.Column("canonical_email", sa.String(length=512), nullable=True),
        sa.Column("canonical_address", sa.JSON(), nullable=True),
        sa.Column("canonical_phone", sa.String(length=64), nullable=True),
        sa.Column("pii_types_found", sa.JSON(), nullable=True),
        sa.Column("source_records", sa.JSON(), nullable=True),
        sa.Column("merge_confidence", sa.Float(), nullable=True),
        sa.Column(
            "notification_required",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "review_status",
            sa.String(length=32),
            server_default=sa.text("'AI_PENDING'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("subject_id"),
    )
    op.create_index(
        "ix_notification_subjects_review_status",
        "notification_subjects",
        ["review_status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_subjects_review_status",
        table_name="notification_subjects",
    )
    op.drop_table("notification_subjects")
