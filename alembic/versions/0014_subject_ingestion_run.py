"""Add ingestion_run_id FK to notification_subjects.

Step 39 #4 — per-job export filtering. Lets exports restrict subjects
to a single ingestion run instead of the whole project. ON DELETE
SET NULL because the subject is valuable evidence even if the job is
later archived or purged.

Revision ID: 0014_subject_ingestion_run
Revises: 0013_merge_explanation
"""
from alembic import op
import sqlalchemy as sa


revision = "0014_subject_ingestion_run"
down_revision = "0013_merge_explanation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_subjects",
        sa.Column("ingestion_run_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_notification_subjects_ingestion_run",
        "notification_subjects",
        "ingestion_runs",
        ["ingestion_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_notification_subjects_ingestion_run_id",
        "notification_subjects",
        ["ingestion_run_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_subjects_ingestion_run_id",
        table_name="notification_subjects",
    )
    op.drop_constraint(
        "fk_notification_subjects_ingestion_run",
        "notification_subjects",
        type_="foreignkey",
    )
    op.drop_column("notification_subjects", "ingestion_run_id")
