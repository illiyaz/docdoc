"""Add extraction_preview column to document_analysis_reviews.

Step 19b: LLM extraction preview stored during analysis phase so
reviewers can see what the LLM will extract before approving.

Revision ID: 0011_extraction_preview
Revises: 0010_subject_lineage
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_extraction_preview"
down_revision = "0010_subject_lineage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_analysis_reviews",
        sa.Column("extraction_preview", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("document_analysis_reviews", "extraction_preview")
