"""Add notification_lists table

Revision ID: 0003_add_notification_lists
Revises: 0002_add_notification_subjects
Create Date: 2026-02-23

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0003_add_notification_lists"
down_revision: str | None = "0002_add_notification_subjects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_lists",
        sa.Column("notification_list_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.String(length=256), nullable=False),
        sa.Column("protocol_id", sa.String(length=128), nullable=False),
        sa.Column("subject_ids", sa.JSON(), nullable=True),
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
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("notification_list_id"),
    )
    op.create_index(
        "ix_notification_lists_status",
        "notification_lists",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_notification_lists_status", table_name="notification_lists")
    op.drop_table("notification_lists")
