"""Initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-02-08

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_path", sa.String(length=1024), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("code_version", sa.String(length=64), nullable=False),
        sa.Column("initiated_by", sa.String(length=128), nullable=False),
        sa.Column("mode", sa.String(length=32), server_default=sa.text("'strict'"), nullable=False),
        sa.Column("status", sa.String(length=32), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ingestion_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_path", sa.String(length=2048), nullable=False),
        sa.Column("file_name", sa.String(length=512), nullable=False),
        sa.Column("file_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=True),
        sa.Column("is_scanned", sa.Boolean(), nullable=True),
        sa.Column("doc_type", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), server_default=sa.text("'discovered'"), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("content_onset_page", sa.Integer(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["ingestion_run_id"], ["ingestion_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ingestion_run_id", "sha256", name="uq_documents_run_sha256"),
    )

    op.create_table(
        "chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("text_start_offset", sa.Integer(), nullable=True),
        sa.Column("text_end_offset", sa.Integer(), nullable=True),
        sa.Column("bbox_map", sa.JSON(), nullable=True),
        sa.Column("ocr_used", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("layout_type", sa.String(length=64), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("page_relevance_score", sa.Float(), nullable=True),
        sa.Column("is_boilerplate", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("page_width", sa.Float(), nullable=True),
        sa.Column("page_height", sa.Float(), nullable=True),
        sa.Column("layout_profile", sa.JSON(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("processing_notes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_chunks_document_chunk_index"),
    )

    op.create_table(
        "detections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=True),
        sa.Column("detection_method", sa.String(length=32), nullable=False),
        sa.Column("rule_name", sa.String(length=128), nullable=True),
        sa.Column("rule_version", sa.String(length=64), nullable=True),
        sa.Column("pii_type", sa.String(length=64), nullable=False),
        sa.Column("sensitivity", sa.String(length=16), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("evidence_page", sa.Integer(), nullable=True),
        sa.Column("evidence_text_start", sa.Integer(), nullable=True),
        sa.Column("evidence_text_end", sa.Integer(), nullable=True),
        sa.Column("evidence_bbox", sa.JSON(), nullable=True),
        sa.Column("is_validated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("validation_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "extractions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=True),
        sa.Column("detection_id", sa.Uuid(), nullable=True),
        sa.Column("pii_type", sa.String(length=64), nullable=False),
        sa.Column("sensitivity", sa.String(length=16), nullable=False),
        sa.Column("normalized_value", sa.String(length=512), nullable=True),
        sa.Column("hashed_value", sa.String(length=128), nullable=False),
        sa.Column("masked_value", sa.String(length=256), nullable=True),
        sa.Column("raw_value_encrypted", sa.Text(), nullable=True),
        sa.Column("normalization_method", sa.String(length=64), nullable=True),
        sa.Column("storage_policy", sa.String(length=32), server_default=sa.text("'hash'"), nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("evidence_page", sa.Integer(), nullable=True),
        sa.Column("evidence_text_start", sa.Integer(), nullable=True),
        sa.Column("evidence_text_end", sa.Integer(), nullable=True),
        sa.Column("evidence_bbox", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["detection_id"], ["detections.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "person_entities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ingestion_run_id", sa.Uuid(), nullable=False),
        sa.Column("entity_hash", sa.String(length=128), nullable=False),
        sa.Column("entity_label", sa.String(length=256), nullable=True),
        sa.Column("is_probabilistic", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("linkage_confidence", sa.Float(), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["ingestion_run_id"], ["ingestion_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ingestion_run_id", "entity_hash", name="uq_person_entities_run_entity_hash"),
    )

    op.create_table(
        "review_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ingestion_run_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("chunk_id", sa.Uuid(), nullable=True),
        sa.Column("task_type", sa.String(length=64), nullable=False),
        sa.Column("priority", sa.String(length=16), server_default=sa.text("'medium'"), nullable=False),
        sa.Column("status", sa.String(length=32), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("assigned_to", sa.String(length=128), nullable=True),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["ingestion_run_id"], ["ingestion_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "review_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("review_task_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reviewer_id", sa.String(length=128), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("corrected_fields", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["review_task_id"], ["review_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "person_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("person_entity_id", sa.Uuid(), nullable=False),
        sa.Column("extraction_id", sa.Uuid(), nullable=False),
        sa.Column("link_method", sa.String(length=32), server_default=sa.text("'deterministic'"), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["extraction_id"], ["extractions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["person_entity_id"], ["person_entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("person_entity_id", "extraction_id", name="uq_person_links_entity_extraction"),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ingestion_run_id", sa.Uuid(), nullable=True),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("actor_type", sa.String(length=32), server_default=sa.text("'system'"), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["ingestion_run_id"], ["ingestion_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index("ix_documents_ingestion_run_id", "documents", ["ingestion_run_id"])
    op.create_index("ix_documents_source_path", "documents", ["source_path"])
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    op.create_index("ix_detections_document_id", "detections", ["document_id"])
    op.create_index("ix_extractions_document_id", "extractions", ["document_id"])
    op.create_index("ix_person_entities_ingestion_run_id", "person_entities", ["ingestion_run_id"])
    op.create_index("ix_review_tasks_ingestion_run_id", "review_tasks", ["ingestion_run_id"])
    op.create_index("ix_audit_events_ingestion_run_id", "audit_events", ["ingestion_run_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_ingestion_run_id", table_name="audit_events")
    op.drop_index("ix_review_tasks_ingestion_run_id", table_name="review_tasks")
    op.drop_index("ix_person_entities_ingestion_run_id", table_name="person_entities")
    op.drop_index("ix_extractions_document_id", table_name="extractions")
    op.drop_index("ix_detections_document_id", table_name="detections")
    op.drop_index("ix_chunks_document_id", table_name="chunks")
    op.drop_index("ix_documents_source_path", table_name="documents")
    op.drop_index("ix_documents_ingestion_run_id", table_name="documents")

    op.drop_table("audit_events")
    op.drop_table("person_links")
    op.drop_table("review_decisions")
    op.drop_table("review_tasks")
    op.drop_table("person_entities")
    op.drop_table("extractions")
    op.drop_table("detections")
    op.drop_table("chunks")
    op.drop_table("documents")
    op.drop_table("ingestion_runs")
