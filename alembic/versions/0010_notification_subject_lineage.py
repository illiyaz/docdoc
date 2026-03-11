"""Add lineage columns to notification_subjects for auditor-ready export.

Step 18: source_document_name, source_page_range, government_id_type,
extraction_confidence, pii_types_list.

Revision ID: 0010_subject_lineage
Revises: 0009_detection_review_decisions
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_subject_lineage"
down_revision = "0009_detection_review_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_subjects",
        sa.Column("source_document_name", sa.String(512), nullable=True),
    )
    op.add_column(
        "notification_subjects",
        sa.Column("source_page_range", sa.String(64), nullable=True),
    )
    op.add_column(
        "notification_subjects",
        sa.Column("government_id_type", sa.String(64), nullable=True),
    )
    op.add_column(
        "notification_subjects",
        sa.Column("extraction_confidence", sa.Float(), nullable=True),
    )
    op.add_column(
        "notification_subjects",
        sa.Column("pii_types_list", sa.String(1024), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notification_subjects", "pii_types_list")
    op.drop_column("notification_subjects", "extraction_confidence")
    op.drop_column("notification_subjects", "government_id_type")
    op.drop_column("notification_subjects", "source_page_range")
    op.drop_column("notification_subjects", "source_document_name")
