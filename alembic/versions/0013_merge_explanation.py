"""Add merge_explanation JSON column to notification_subjects.

Step 27 — Critical #2: Stores field-level match signals explaining
why records were merged, so auditors can understand merge decisions.

Revision ID: 0013
Revises: 0012
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_merge_explanation"
down_revision = "0012_subject_dob_gov_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_subjects",
        sa.Column("merge_explanation", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notification_subjects", "merge_explanation")
