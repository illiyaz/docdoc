from sqlalchemy import create_engine, inspect

from app.db.base import Base
from app.db import models  # noqa: F401


def _assert_default_contains(default_value: object, expected: str) -> None:
    assert default_value is not None
    normalized = str(default_value).lower().replace("(", "").replace(")", "").replace("'", "").strip()
    assert expected in normalized


def test_schema_creation_in_sqlite_includes_all_canonical_tables():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    expected = {
        "ingestion_runs",
        "documents",
        "chunks",
        "detections",
        "extractions",
        "person_entities",
        "person_links",
        "review_tasks",
        "review_decisions",
        "audit_events",
        "notification_subjects",
        "notification_lists",
        # Phase 5 — Forentis AI
        "projects",
        "protocol_configs",
        "density_summaries",
        "export_jobs",
        "llm_call_logs",
    }
    assert expected.issubset(table_names)


def test_document_and_chunk_required_columns_exist():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)

    ingestion_run_columns = {column["name"]: column for column in inspector.get_columns("ingestion_runs")}
    assert "config_hash" in ingestion_run_columns
    assert ingestion_run_columns["config_hash"]["nullable"] is False
    assert "code_version" in ingestion_run_columns
    assert ingestion_run_columns["code_version"]["nullable"] is False
    assert "initiated_by" in ingestion_run_columns
    assert ingestion_run_columns["initiated_by"]["nullable"] is False
    # Phase 5: project_id FK — nullable for backward compat
    assert "project_id" in ingestion_run_columns
    assert ingestion_run_columns["project_id"]["nullable"] is True

    document_columns = {column["name"]: column for column in inspector.get_columns("documents")}
    assert "content_onset_page" in document_columns
    assert document_columns["content_onset_page"]["nullable"] is True
    assert "file_type" in document_columns
    assert "language" in document_columns
    assert "is_scanned" in document_columns
    assert "doc_type" in document_columns
    assert "status" in document_columns
    # Phase 5: cataloging fields
    assert "structure_class" in document_columns
    assert document_columns["structure_class"]["nullable"] is True
    assert "can_auto_process" in document_columns
    assert document_columns["can_auto_process"]["nullable"] is False
    assert "manual_review_reason" in document_columns
    assert document_columns["manual_review_reason"]["nullable"] is True

    chunk_columns = {column["name"]: column for column in inspector.get_columns("chunks")}
    assert "text" in chunk_columns
    assert chunk_columns["text"]["nullable"] is False
    assert "bbox_map" in chunk_columns
    assert "ocr_used" in chunk_columns
    assert chunk_columns["ocr_used"]["nullable"] is False
    assert "layout_type" in chunk_columns
    assert "confidence" in chunk_columns
    assert "page_relevance_score" in chunk_columns
    assert chunk_columns["page_relevance_score"]["nullable"] is True

    assert "is_boilerplate" in chunk_columns
    assert chunk_columns["is_boilerplate"]["nullable"] is False
    assert chunk_columns["is_boilerplate"]["default"] is not None

    assert "page_width" in chunk_columns
    assert chunk_columns["page_width"]["nullable"] is True

    assert "page_height" in chunk_columns
    assert chunk_columns["page_height"]["nullable"] is True

    assert "layout_profile" in chunk_columns
    assert chunk_columns["layout_profile"]["nullable"] is True

    extraction_columns = {column["name"]: column for column in inspector.get_columns("extractions")}
    assert "storage_policy" in extraction_columns
    assert "retention_until" in extraction_columns
    assert extraction_columns["retention_until"]["nullable"] is True


def test_server_defaults_exist_for_core_state_fields():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)

    ingestion_run_columns = {column["name"]: column for column in inspector.get_columns("ingestion_runs")}
    _assert_default_contains(ingestion_run_columns["mode"]["default"], "strict")
    _assert_default_contains(ingestion_run_columns["status"]["default"], "pending")

    document_columns = {column["name"]: column for column in inspector.get_columns("documents")}
    _assert_default_contains(document_columns["status"]["default"], "discovered")
    _assert_default_contains(document_columns["can_auto_process"]["default"], "true")

    review_task_columns = {column["name"]: column for column in inspector.get_columns("review_tasks")}
    _assert_default_contains(review_task_columns["status"]["default"], "pending")

    person_link_columns = {column["name"]: column for column in inspector.get_columns("person_links")}
    _assert_default_contains(person_link_columns["link_method"]["default"], "deterministic")

    audit_event_columns = {column["name"]: column for column in inspector.get_columns("audit_events")}
    _assert_default_contains(audit_event_columns["actor"]["default"], "system")
    _assert_default_contains(audit_event_columns["immutable"]["default"], "true")

    extraction_columns = {column["name"]: column for column in inspector.get_columns("extractions")}
    _assert_default_contains(extraction_columns["storage_policy"]["default"], "hash")

    ns_columns = {column["name"]: column for column in inspector.get_columns("notification_subjects")}
    _assert_default_contains(ns_columns["review_status"]["default"], "ai_pending")
    _assert_default_contains(ns_columns["notification_required"]["default"], "false")

    nl_columns = {column["name"]: column for column in inspector.get_columns("notification_lists")}
    _assert_default_contains(nl_columns["status"]["default"], "pending")

    # Phase 5 defaults
    project_columns = {column["name"]: column for column in inspector.get_columns("projects")}
    _assert_default_contains(project_columns["status"]["default"], "active")

    pc_columns = {column["name"]: column for column in inspector.get_columns("protocol_configs")}
    _assert_default_contains(pc_columns["status"]["default"], "draft")

    ej_columns = {column["name"]: column for column in inspector.get_columns("export_jobs")}
    _assert_default_contains(ej_columns["status"]["default"], "pending")


def test_notification_subject_columns_exist():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)

    ns_columns = {column["name"]: column for column in inspector.get_columns("notification_subjects")}

    # Primary key
    assert "subject_id" in ns_columns
    assert ns_columns["subject_id"]["nullable"] is False

    # Phase 5: project_id FK
    assert "project_id" in ns_columns
    assert ns_columns["project_id"]["nullable"] is True

    # Canonical contact fields — all nullable
    assert "canonical_name" in ns_columns
    assert ns_columns["canonical_name"]["nullable"] is True
    assert "canonical_email" in ns_columns
    assert ns_columns["canonical_email"]["nullable"] is True
    assert "canonical_address" in ns_columns
    assert ns_columns["canonical_address"]["nullable"] is True
    assert "canonical_phone" in ns_columns
    assert ns_columns["canonical_phone"]["nullable"] is True

    # JSON arrays — nullable
    assert "pii_types_found" in ns_columns
    assert ns_columns["pii_types_found"]["nullable"] is True
    assert "source_records" in ns_columns
    assert ns_columns["source_records"]["nullable"] is True

    # Numeric confidence — nullable
    assert "merge_confidence" in ns_columns
    assert ns_columns["merge_confidence"]["nullable"] is True

    # Required fields with server defaults
    assert "notification_required" in ns_columns
    assert ns_columns["notification_required"]["nullable"] is False
    assert "review_status" in ns_columns
    assert ns_columns["review_status"]["nullable"] is False

    # Audit timestamp
    assert "created_at" in ns_columns
    assert ns_columns["created_at"]["nullable"] is False


def test_notification_list_columns_exist():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)

    nl_columns = {column["name"]: column for column in inspector.get_columns("notification_lists")}

    # Primary key
    assert "notification_list_id" in nl_columns
    assert nl_columns["notification_list_id"]["nullable"] is False

    # Phase 5: project_id FK
    assert "project_id" in nl_columns
    assert nl_columns["project_id"]["nullable"] is True

    # Required fields
    assert "job_id" in nl_columns
    assert nl_columns["job_id"]["nullable"] is False
    assert "protocol_id" in nl_columns
    assert nl_columns["protocol_id"]["nullable"] is False

    # JSON array — nullable
    assert "subject_ids" in nl_columns
    assert nl_columns["subject_ids"]["nullable"] is True

    # Status with server default
    assert "status" in nl_columns
    assert nl_columns["status"]["nullable"] is False

    # Timestamps
    assert "created_at" in nl_columns
    assert nl_columns["created_at"]["nullable"] is False
    assert "approved_at" in nl_columns
    assert nl_columns["approved_at"]["nullable"] is True

    # Approval fields — nullable
    assert "approved_by" in nl_columns
    assert nl_columns["approved_by"]["nullable"] is True


def test_audit_event_columns_exist():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)

    cols = {c["name"]: c for c in inspector.get_columns("audit_events")}

    # Primary key
    assert "audit_event_id" in cols
    assert cols["audit_event_id"]["nullable"] is False

    # Required fields
    assert "event_type" in cols
    assert cols["event_type"]["nullable"] is False
    assert "actor" in cols
    assert cols["actor"]["nullable"] is False

    # Nullable reference fields
    assert "subject_id" in cols
    assert cols["subject_id"]["nullable"] is True
    assert "pii_record_id" in cols
    assert cols["pii_record_id"]["nullable"] is True

    # Decision fields — all nullable
    assert "decision" in cols
    assert cols["decision"]["nullable"] is True
    assert "rationale" in cols
    assert cols["rationale"]["nullable"] is True
    assert "regulatory_basis" in cols
    assert cols["regulatory_basis"]["nullable"] is True

    # Timestamp with server default
    assert "timestamp" in cols
    assert cols["timestamp"]["nullable"] is False

    # Immutable flag with server default
    assert "immutable" in cols
    assert cols["immutable"]["nullable"] is False


def test_review_task_columns_exist():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)

    cols = {c["name"]: c for c in inspector.get_columns("review_tasks")}

    # Primary key
    assert "review_task_id" in cols
    assert cols["review_task_id"]["nullable"] is False

    # Required fields
    assert "queue_type" in cols
    assert cols["queue_type"]["nullable"] is False
    assert "required_role" in cols
    assert cols["required_role"]["nullable"] is False

    # FK to notification_subjects — nullable
    assert "subject_id" in cols
    assert cols["subject_id"]["nullable"] is True

    # Assignee — nullable
    assert "assigned_to" in cols
    assert cols["assigned_to"]["nullable"] is True

    # Status with server default
    assert "status" in cols
    assert cols["status"]["nullable"] is False

    # Timestamps
    assert "created_at" in cols
    assert cols["created_at"]["nullable"] is False
    assert "completed_at" in cols
    assert cols["completed_at"]["nullable"] is True


def test_project_columns_exist():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)

    cols = {c["name"]: c for c in inspector.get_columns("projects")}

    assert "id" in cols
    assert cols["id"]["nullable"] is False
    assert "name" in cols
    assert cols["name"]["nullable"] is False
    assert "description" in cols
    assert cols["description"]["nullable"] is True
    assert "status" in cols
    assert cols["status"]["nullable"] is False
    assert "created_by" in cols
    assert cols["created_by"]["nullable"] is True
    assert "created_at" in cols
    assert cols["created_at"]["nullable"] is False
    assert "updated_at" in cols
    assert cols["updated_at"]["nullable"] is False


def test_protocol_config_columns_exist():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)

    cols = {c["name"]: c for c in inspector.get_columns("protocol_configs")}

    assert "id" in cols
    assert cols["id"]["nullable"] is False
    assert "project_id" in cols
    assert cols["project_id"]["nullable"] is False
    assert "base_protocol_id" in cols
    assert cols["base_protocol_id"]["nullable"] is True
    assert "name" in cols
    assert cols["name"]["nullable"] is False
    assert "config_json" in cols
    assert "status" in cols
    assert cols["status"]["nullable"] is False
    assert "created_at" in cols
    assert cols["created_at"]["nullable"] is False
    assert "updated_at" in cols
    assert cols["updated_at"]["nullable"] is False


def test_density_summary_columns_exist():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)

    cols = {c["name"]: c for c in inspector.get_columns("density_summaries")}

    assert "id" in cols
    assert cols["id"]["nullable"] is False
    assert "project_id" in cols
    assert cols["project_id"]["nullable"] is False
    assert "document_id" in cols
    assert cols["document_id"]["nullable"] is True
    assert "total_entities" in cols
    assert cols["total_entities"]["nullable"] is False
    assert "by_category" in cols
    assert cols["by_category"]["nullable"] is True
    assert "by_type" in cols
    assert cols["by_type"]["nullable"] is True
    assert "confidence" in cols
    assert cols["confidence"]["nullable"] is True
    assert "confidence_notes" in cols
    assert cols["confidence_notes"]["nullable"] is True
    assert "created_at" in cols
    assert cols["created_at"]["nullable"] is False


def test_export_job_columns_exist():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)

    cols = {c["name"]: c for c in inspector.get_columns("export_jobs")}

    assert "id" in cols
    assert cols["id"]["nullable"] is False
    assert "project_id" in cols
    assert cols["project_id"]["nullable"] is False
    assert "protocol_config_id" in cols
    assert cols["protocol_config_id"]["nullable"] is True
    assert "export_type" in cols
    assert cols["export_type"]["nullable"] is False
    assert "status" in cols
    assert cols["status"]["nullable"] is False
    assert "file_path" in cols
    assert cols["file_path"]["nullable"] is True
    assert "row_count" in cols
    assert cols["row_count"]["nullable"] is True
    assert "filters_json" in cols
    assert cols["filters_json"]["nullable"] is True
    assert "created_at" in cols
    assert cols["created_at"]["nullable"] is False
    assert "completed_at" in cols
    assert cols["completed_at"]["nullable"] is True


def test_llm_call_log_columns_exist():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)

    cols = {c["name"]: c for c in inspector.get_columns("llm_call_logs")}

    assert "id" in cols
    assert cols["id"]["nullable"] is False
    assert "document_id" in cols
    assert cols["document_id"]["nullable"] is True
    assert "use_case" in cols
    assert cols["use_case"]["nullable"] is False
    assert "model" in cols
    assert cols["model"]["nullable"] is False
    assert "prompt_text" in cols
    assert cols["prompt_text"]["nullable"] is False
    assert "response_text" in cols
    assert cols["response_text"]["nullable"] is False
    assert "decision" in cols
    assert cols["decision"]["nullable"] is True
    assert "accepted" in cols
    assert cols["accepted"]["nullable"] is True
    assert "latency_ms" in cols
    assert cols["latency_ms"]["nullable"] is True
    assert "token_count" in cols
    assert cols["token_count"]["nullable"] is True
    assert "created_at" in cols
    assert cols["created_at"]["nullable"] is False
