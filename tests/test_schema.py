from sqlalchemy import create_engine, inspect

from app.db.base import Base
from app.db import models  # noqa: F401


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

    document_columns = {column["name"]: column for column in inspector.get_columns("documents")}
    assert "content_onset_page" in document_columns
    assert document_columns["content_onset_page"]["nullable"] is True
    assert "file_type" in document_columns
    assert "language" in document_columns
    assert "is_scanned" in document_columns
    assert "doc_type" in document_columns
    assert "status" in document_columns

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
