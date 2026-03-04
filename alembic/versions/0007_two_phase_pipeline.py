"""Add two-phase pipeline support.

- New table: document_analysis_reviews
- documents: analysis_phase_status, sample_onset_page, sample_extraction_count
- ingestion_runs: pipeline_mode, analysis_completed_at
- extractions: is_sample

Revision ID: 0007_two_phase_pipeline
Revises: 0006_document_structure_analysis
Create Date: 2026-03-03

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0007_two_phase_pipeline"
down_revision: str | None = "0006_document_structure_analysis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- new table: document_analysis_reviews --------------------------------
    op.create_table(
        "document_analysis_reviews",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ingestion_run_id", sa.Uuid(), sa.ForeignKey("ingestion_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'pending_review'")),
        sa.Column("reviewer_id", sa.String(length=128), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("auto_approve_reason", sa.Text(), nullable=True),
        sa.Column("sample_confidence_avg", sa.Float(), nullable=True),
        sa.Column("sample_confidence_min", sa.Float(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # -- documents: add two-phase columns ------------------------------------
    op.add_column("documents", sa.Column("analysis_phase_status", sa.String(length=32), nullable=True))
    op.add_column("documents", sa.Column("sample_onset_page", sa.Integer(), nullable=True))
    op.add_column("documents", sa.Column("sample_extraction_count", sa.Integer(), nullable=True))

    # -- ingestion_runs: add pipeline mode and analysis timestamp ------------
    op.add_column(
        "ingestion_runs",
        sa.Column("pipeline_mode", sa.String(length=32), nullable=False, server_default=sa.text("'full'")),
    )
    op.add_column("ingestion_runs", sa.Column("analysis_completed_at", sa.DateTime(timezone=True), nullable=True))

    # -- extractions: add sample flag ----------------------------------------
    op.add_column(
        "extractions",
        sa.Column("is_sample", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("extractions", "is_sample")
    op.drop_column("ingestion_runs", "analysis_completed_at")
    op.drop_column("ingestion_runs", "pipeline_mode")
    op.drop_column("documents", "sample_extraction_count")
    op.drop_column("documents", "sample_onset_page")
    op.drop_column("documents", "analysis_phase_status")
    op.drop_table("document_analysis_reviews")
