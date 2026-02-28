from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func, text as sql_text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default=sql_text("'active'")
    )
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    protocol_configs: Mapped[list[ProtocolConfig]] = relationship(back_populates="project")
    ingestion_runs: Mapped[list[IngestionRun]] = relationship(back_populates="project")
    density_summaries: Mapped[list[DensitySummary]] = relationship(back_populates="project")
    export_jobs: Mapped[list[ExportJob]] = relationship(back_populates="project")


class ProtocolConfig(Base):
    __tablename__ = "protocol_configs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    base_protocol_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="draft", server_default=sql_text("'draft'")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    project: Mapped[Project] = relationship(back_populates="protocol_configs")


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    source_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    code_version: Mapped[str] = mapped_column(String(64), nullable=False)
    initiated_by: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="strict", server_default=sql_text("'strict'"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default=sql_text("'pending'"))
    config_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    project: Mapped[Project | None] = relationship(back_populates="ingestion_runs")
    documents: Mapped[list[Document]] = relationship(back_populates="ingestion_run")
    person_entities: Mapped[list[PersonEntity]] = relationship(back_populates="ingestion_run")


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("ingestion_run_id", "sha256", name="uq_documents_run_sha256"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    ingestion_run_id: Mapped[UUID] = mapped_column(ForeignKey("ingestion_runs.id", ondelete="CASCADE"), nullable=False)
    source_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    file_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_scanned: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    doc_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="discovered", server_default=sql_text("'discovered'")
    )
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_onset_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    structure_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    can_auto_process: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sql_text("true")
    )
    manual_review_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    ingestion_run: Mapped[IngestionRun] = relationship(back_populates="documents")
    chunks: Mapped[list[Chunk]] = relationship(back_populates="document")
    detections: Mapped[list[Detection]] = relationship(back_populates="document")
    extractions: Mapped[list[Extraction]] = relationship(back_populates="document")


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index", name="uq_chunks_document_chunk_index"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=sql_text("''"))
    text_start_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text_end_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bbox_map: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ocr_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sql_text("false"))
    layout_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    page_relevance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_boilerplate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sql_text("false")
    )
    page_width: Mapped[float | None] = mapped_column(Float, nullable=True)
    page_height: Mapped[float | None] = mapped_column(Float, nullable=True)
    layout_profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processing_notes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    document: Mapped[Document] = relationship(back_populates="chunks")
    detections: Mapped[list[Detection]] = relationship(back_populates="chunk")
    extractions: Mapped[list[Extraction]] = relationship(back_populates="chunk")


class Detection(Base):
    __tablename__ = "detections"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    chunk_id: Mapped[UUID | None] = mapped_column(ForeignKey("chunks.id", ondelete="SET NULL"), nullable=True)
    detection_method: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rule_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pii_type: Mapped[str] = mapped_column(String(64), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_text_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_text_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_bbox: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_validated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sql_text("false")
    )
    validation_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    document: Mapped[Document] = relationship(back_populates="detections")
    chunk: Mapped[Chunk | None] = relationship(back_populates="detections")
    extractions: Mapped[list[Extraction]] = relationship(back_populates="detection")


class Extraction(Base):
    __tablename__ = "extractions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    chunk_id: Mapped[UUID | None] = mapped_column(ForeignKey("chunks.id", ondelete="SET NULL"), nullable=True)
    detection_id: Mapped[UUID | None] = mapped_column(ForeignKey("detections.id", ondelete="SET NULL"), nullable=True)
    pii_type: Mapped[str] = mapped_column(String(64), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False)
    normalized_value: Mapped[str | None] = mapped_column(String(512), nullable=True)
    hashed_value: Mapped[str] = mapped_column(String(128), nullable=False)
    masked_value: Mapped[str | None] = mapped_column(String(256), nullable=True)
    raw_value_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalization_method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    storage_policy: Mapped[str] = mapped_column(
        String(32), nullable=False, default="hash", server_default=sql_text("'hash'")
    )
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_text_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_text_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_bbox: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="extractions")
    chunk: Mapped[Chunk | None] = relationship(back_populates="extractions")
    detection: Mapped[Detection | None] = relationship(back_populates="extractions")
    person_links: Mapped[list[PersonLink]] = relationship(back_populates="extraction")


class PersonEntity(Base):
    __tablename__ = "person_entities"
    __table_args__ = (UniqueConstraint("ingestion_run_id", "entity_hash", name="uq_person_entities_run_entity_hash"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    ingestion_run_id: Mapped[UUID] = mapped_column(ForeignKey("ingestion_runs.id", ondelete="CASCADE"), nullable=False)
    entity_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_probabilistic: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sql_text("false")
    )
    linkage_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    attributes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    ingestion_run: Mapped[IngestionRun] = relationship(back_populates="person_entities")
    links: Mapped[list[PersonLink]] = relationship(back_populates="person_entity")


class PersonLink(Base):
    __tablename__ = "person_links"
    __table_args__ = (UniqueConstraint("person_entity_id", "extraction_id", name="uq_person_links_entity_extraction"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    person_entity_id: Mapped[UUID] = mapped_column(ForeignKey("person_entities.id", ondelete="CASCADE"), nullable=False)
    extraction_id: Mapped[UUID] = mapped_column(ForeignKey("extractions.id", ondelete="CASCADE"), nullable=False)
    link_method: Mapped[str] = mapped_column(
        String(32), nullable=False, default="deterministic", server_default=sql_text("'deterministic'")
    )
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sql_text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    person_entity: Mapped[PersonEntity] = relationship(back_populates="links")
    extraction: Mapped[Extraction] = relationship(back_populates="person_links")


class ReviewTask(Base):
    """Phase 4 HITL review task — one per subject-queue combination.

    Four queue types: low_confidence, escalation, qc_sampling, rra_review.
    """

    __tablename__ = "review_tasks"

    review_task_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    queue_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("notification_subjects.subject_id", ondelete="SET NULL"), nullable=True,
    )
    assigned_to: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING", server_default=sql_text("'PENDING'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    required_role: Mapped[str] = mapped_column(String(32), nullable=False)

    decisions: Mapped[list[ReviewDecision]] = relationship(back_populates="review_task")


class ReviewDecision(Base):
    __tablename__ = "review_decisions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    review_task_id: Mapped[UUID] = mapped_column(
        ForeignKey("review_tasks.review_task_id", ondelete="CASCADE"), nullable=False,
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrected_fields: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    review_task: Mapped[ReviewTask] = relationship(back_populates="decisions")


class NotificationSubject(Base):
    """One row per unique individual identified across all source documents.

    Produced by the Phase 2 RRA pipeline.  Each subject aggregates all
    PII types found, normalized contact information, and provenance links
    back to the originating extraction records.
    """

    __tablename__ = "notification_subjects"

    subject_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    canonical_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    canonical_email: Mapped[str | None] = mapped_column(String(512), nullable=True)
    canonical_address: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    canonical_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pii_types_found: Mapped[list | None] = mapped_column(JSON, nullable=True)
    source_records: Mapped[list | None] = mapped_column(JSON, nullable=True)
    merge_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    notification_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sql_text("false")
    )
    review_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="AI_PENDING",
        server_default=sql_text("'AI_PENDING'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class NotificationList(Base):
    """A notification list produced by applying a Protocol to approved subjects.

    Created in Phase 3 by the notification list builder.  Delivery is
    gated on ``status = 'APPROVED'``.
    """

    __tablename__ = "notification_lists"

    notification_list_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    job_id: Mapped[str] = mapped_column(String(256), nullable=False)
    protocol_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING", server_default=sql_text("'PENDING'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)


class AuditEvent(Base):
    """Phase 4 append-only audit log.

    Records every pipeline and human-review event for regulatory defensibility.
    Rows are immutable by default — ``immutable=True``.
    """

    __tablename__ = "audit_events"

    audit_event_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(
        String(128), nullable=False, default="system", server_default=sql_text("'system'"),
    )
    subject_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pii_record_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    regulatory_basis: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    immutable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sql_text("true"),
    )


class DensitySummary(Base):
    """PII density summary for a project or individual document.

    ``by_category`` uses the expanded 8-category taxonomy.  A single entity
    may contribute to multiple categories, so the sum of category counts can
    exceed ``total_entities``.

    Example ``by_category``::

        {
            "PII": 12,
            "SPII": 5,
            "PHI": 3,
            "PFI": 4,
            "PCI": 2,
            "NPI": 3,
            "FTI": 1,
            "CREDENTIALS": 0
        }

    Categories:
        PII         Personally Identifiable Information (baseline)
        SPII        Sensitive PII (SSN, biometrics, government IDs)
        PHI         Protected Health Information (HIPAA)
        PFI         Personal Financial Information (GLBA)
        PCI         Payment Card Industry Data (PCI-DSS)
        NPI         Nonpublic Personal Information (GLBA / banking)
        FTI         Federal Tax Information (IRS 1075)
        CREDENTIALS Authentication secrets (passwords, API keys)
    """

    __tablename__ = "density_summaries"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    document_id: Mapped[UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=True)
    total_entities: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    by_category: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    by_type: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    confidence_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    project: Mapped[Project] = relationship(back_populates="density_summaries")


class ExportJob(Base):
    __tablename__ = "export_jobs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    protocol_config_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("protocol_configs.id", ondelete="SET NULL"), nullable=True,
    )
    export_type: Mapped[str] = mapped_column(String(32), nullable=False, default="csv")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default=sql_text("'pending'"),
    )
    file_path: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    filters_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project] = relationship(back_populates="export_jobs")


class LLMCallLog(Base):
    __tablename__ = "llm_call_logs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_id: Mapped[UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)
    use_case: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    response_text: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    accepted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
