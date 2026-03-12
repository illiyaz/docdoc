"""Add canonical_dob and canonical_government_id to notification_subjects.

Revision ID: 0012
Revises: 0011
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_subject_dob_gov_id"
down_revision = "0011_extraction_preview"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_subjects",
        sa.Column("canonical_dob", sa.String(64), nullable=True),
    )
    op.add_column(
        "notification_subjects",
        sa.Column("canonical_government_id", sa.String(128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notification_subjects", "canonical_government_id")
    op.drop_column("notification_subjects", "canonical_dob")
