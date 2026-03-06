"""Add detection_review_decisions table and selected_entity_types column.

Step 15: Field-level detection review + protocol mapping.

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New table: detection_review_decisions
    op.create_table(
        "detection_review_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_analysis_review_id", sa.Uuid(), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("detected_value_masked", sa.String(256), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("include_in_extraction", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("decision_reason", sa.String(256), nullable=True),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_source", sa.String(16), nullable=False, server_default=sa.text("'default'")),
        sa.ForeignKeyConstraint(
            ["document_analysis_review_id"],
            ["document_analysis_reviews.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # Extend document_analysis_reviews with selected_entity_types
    op.add_column(
        "document_analysis_reviews",
        sa.Column("selected_entity_types", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("document_analysis_reviews", "selected_entity_types")
    op.drop_table("detection_review_decisions")
