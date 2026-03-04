"""Add entity_analysis JSON column to documents.

Stores LLM entity relationship analysis results (entity groups,
relationships, extraction guidance) for the two-phase pipeline.

Revision ID: 0008_entity_analysis
Revises: 0007_two_phase_pipeline
Create Date: 2026-03-04

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0008_entity_analysis"
down_revision: str | None = "0007_two_phase_pipeline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("entity_analysis", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "entity_analysis")
