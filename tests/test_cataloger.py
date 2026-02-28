"""Tests for the cataloger task (Step 3 — file structure classifier).

Covers:
- classify_extension() pure function for every supported extension
- classify_extension() returns non-extractable for unknown extensions
- CatalogerTask.run() populates structure_class, can_auto_process,
  manual_review_reason on Document ORM objects
- CatalogerTask sets can_auto_process=False for non-extractable files
- CatalogerTask sets can_auto_process=True for all known extensions
- Integration: documents persisted to DB with correct catalog fields
- Catalog-summary endpoint reflects cataloger output
"""
from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import Document, IngestionRun
from app.tasks.cataloger import (
    CatalogerTask,
    StructureClass,
    _ALL_KNOWN_EXTENSIONS,
    _SEMI_STRUCTURED_EXTENSIONS,
    _STRUCTURED_EXTENSIONS,
    _UNSTRUCTURED_EXTENSIONS,
    classify_extension,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session():
    """In-memory SQLite session with all tables created."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = SessionLocal()
    yield session
    session.close()


def _make_ingestion_run(db: Session) -> IngestionRun:
    run = IngestionRun(
        id=uuid4(),
        source_path="/tmp/test",
        config_hash="abc123",
        code_version="1.0.0",
        initiated_by="test",
        mode="strict",
        status="pending",
    )
    db.add(run)
    db.flush()
    return run


def _make_document(
    db: Session,
    ingestion_run_id,
    file_name: str = "test.pdf",
    file_type: str = "pdf",
) -> Document:
    sha = hashlib.sha256(f"{file_name}-{uuid4()}".encode()).hexdigest()
    doc = Document(
        id=uuid4(),
        ingestion_run_id=ingestion_run_id,
        source_path=f"/tmp/{file_name}",
        file_name=file_name,
        file_type=file_type,
        size_bytes=1024,
        sha256=sha,
        status="discovered",
    )
    db.add(doc)
    db.flush()
    return doc


# ===========================================================================
# classify_extension() — pure function tests
# ===========================================================================


class TestClassifyExtension:
    """Verify every known extension maps to the correct structure class."""

    @pytest.mark.parametrize("ext", sorted(_STRUCTURED_EXTENSIONS))
    def test_structured_extensions(self, ext: str) -> None:
        assert classify_extension(ext) == "structured"

    @pytest.mark.parametrize("ext", sorted(_SEMI_STRUCTURED_EXTENSIONS))
    def test_semi_structured_extensions(self, ext: str) -> None:
        assert classify_extension(ext) == "semi-structured"

    @pytest.mark.parametrize("ext", sorted(_UNSTRUCTURED_EXTENSIONS))
    def test_unstructured_extensions(self, ext: str) -> None:
        assert classify_extension(ext) == "unstructured"

    @pytest.mark.parametrize("ext", ["dat", "txt", "zip", "exe", "bin", "jpg", "png"])
    def test_non_extractable_extensions(self, ext: str) -> None:
        assert classify_extension(ext) == "non-extractable"

    def test_empty_string(self) -> None:
        assert classify_extension("") == "non-extractable"

    def test_case_insensitive(self) -> None:
        assert classify_extension("PDF") == "unstructured"
        assert classify_extension("CSV") == "structured"
        assert classify_extension("HTML") == "semi-structured"

    def test_whitespace_stripped(self) -> None:
        assert classify_extension("  pdf  ") == "unstructured"

    def test_all_known_is_union(self) -> None:
        """_ALL_KNOWN_EXTENSIONS is the union of the three categories."""
        assert _ALL_KNOWN_EXTENSIONS == (
            _STRUCTURED_EXTENSIONS | _SEMI_STRUCTURED_EXTENSIONS | _UNSTRUCTURED_EXTENSIONS
        )


# ===========================================================================
# CatalogerTask.run() — ORM integration
# ===========================================================================


class TestCatalogerTask:
    """Verify CatalogerTask populates catalog fields on Document objects."""

    def test_pdf_classified_as_unstructured(self, db_session: Session) -> None:
        run = _make_ingestion_run(db_session)
        doc = _make_document(db_session, run.id, "report.pdf", "pdf")
        CatalogerTask(db_session).run([doc])
        assert doc.structure_class == "unstructured"
        assert doc.can_auto_process is True
        assert doc.manual_review_reason is None

    def test_csv_classified_as_structured(self, db_session: Session) -> None:
        run = _make_ingestion_run(db_session)
        doc = _make_document(db_session, run.id, "data.csv", "csv")
        CatalogerTask(db_session).run([doc])
        assert doc.structure_class == "structured"
        assert doc.can_auto_process is True

    def test_xlsx_classified_as_structured(self, db_session: Session) -> None:
        run = _make_ingestion_run(db_session)
        doc = _make_document(db_session, run.id, "data.xlsx", "xlsx")
        CatalogerTask(db_session).run([doc])
        assert doc.structure_class == "structured"
        assert doc.can_auto_process is True

    def test_html_classified_as_semi_structured(self, db_session: Session) -> None:
        run = _make_ingestion_run(db_session)
        doc = _make_document(db_session, run.id, "page.html", "html")
        CatalogerTask(db_session).run([doc])
        assert doc.structure_class == "semi-structured"
        assert doc.can_auto_process is True

    def test_eml_classified_as_semi_structured(self, db_session: Session) -> None:
        run = _make_ingestion_run(db_session)
        doc = _make_document(db_session, run.id, "email.eml", "eml")
        CatalogerTask(db_session).run([doc])
        assert doc.structure_class == "semi-structured"
        assert doc.can_auto_process is True

    def test_docx_classified_as_unstructured(self, db_session: Session) -> None:
        run = _make_ingestion_run(db_session)
        doc = _make_document(db_session, run.id, "doc.docx", "docx")
        CatalogerTask(db_session).run([doc])
        assert doc.structure_class == "unstructured"
        assert doc.can_auto_process is True

    def test_dat_classified_as_non_extractable(self, db_session: Session) -> None:
        run = _make_ingestion_run(db_session)
        doc = _make_document(db_session, run.id, "binary.dat", "dat")
        CatalogerTask(db_session).run([doc])
        assert doc.structure_class == "non-extractable"
        assert doc.can_auto_process is False
        assert doc.manual_review_reason is not None
        assert ".dat" in doc.manual_review_reason

    def test_unknown_extension_non_extractable(self, db_session: Session) -> None:
        run = _make_ingestion_run(db_session)
        doc = _make_document(db_session, run.id, "file.xyz", "xyz")
        CatalogerTask(db_session).run([doc])
        assert doc.structure_class == "non-extractable"
        assert doc.can_auto_process is False
        assert doc.manual_review_reason is not None

    def test_empty_file_type_non_extractable(self, db_session: Session) -> None:
        run = _make_ingestion_run(db_session)
        doc = _make_document(db_session, run.id, "noext", "")
        CatalogerTask(db_session).run([doc])
        assert doc.structure_class == "non-extractable"
        assert doc.can_auto_process is False
        assert "no recognized extension" in doc.manual_review_reason

    def test_mixed_batch(self, db_session: Session) -> None:
        """Process a batch with all four structure classes."""
        run = _make_ingestion_run(db_session)
        docs = [
            _make_document(db_session, run.id, "a.csv", "csv"),
            _make_document(db_session, run.id, "b.html", "html"),
            _make_document(db_session, run.id, "c.pdf", "pdf"),
            _make_document(db_session, run.id, "d.dat", "dat"),
        ]
        result = CatalogerTask(db_session).run(docs)
        assert len(result) == 4
        assert result[0].structure_class == "structured"
        assert result[1].structure_class == "semi-structured"
        assert result[2].structure_class == "unstructured"
        assert result[3].structure_class == "non-extractable"

    def test_empty_list(self, db_session: Session) -> None:
        """Running on an empty list should not fail."""
        result = CatalogerTask(db_session).run([])
        assert result == []

    def test_fields_persisted_in_db(self, db_session: Session) -> None:
        """After run(), fields are visible when re-querying the document."""
        run = _make_ingestion_run(db_session)
        doc = _make_document(db_session, run.id, "data.parquet", "parquet")
        CatalogerTask(db_session).run([doc])
        db_session.commit()

        refreshed = db_session.get(Document, doc.id)
        assert refreshed.structure_class == "structured"
        assert refreshed.can_auto_process is True
        assert refreshed.manual_review_reason is None

    def test_non_extractable_fields_persisted(self, db_session: Session) -> None:
        """Non-extractable catalog fields are persisted in DB."""
        run = _make_ingestion_run(db_session)
        doc = _make_document(db_session, run.id, "image.jpg", "jpg")
        CatalogerTask(db_session).run([doc])
        db_session.commit()

        refreshed = db_session.get(Document, doc.id)
        assert refreshed.structure_class == "non-extractable"
        assert refreshed.can_auto_process is False
        assert refreshed.manual_review_reason is not None

    def test_parquet_structured(self, db_session: Session) -> None:
        run = _make_ingestion_run(db_session)
        doc = _make_document(db_session, run.id, "data.parquet", "parquet")
        CatalogerTask(db_session).run([doc])
        assert doc.structure_class == "structured"

    def test_avro_structured(self, db_session: Session) -> None:
        run = _make_ingestion_run(db_session)
        doc = _make_document(db_session, run.id, "data.avro", "avro")
        CatalogerTask(db_session).run([doc])
        assert doc.structure_class == "structured"

    def test_xls_structured(self, db_session: Session) -> None:
        run = _make_ingestion_run(db_session)
        doc = _make_document(db_session, run.id, "legacy.xls", "xls")
        CatalogerTask(db_session).run([doc])
        assert doc.structure_class == "structured"

    def test_htm_semi_structured(self, db_session: Session) -> None:
        run = _make_ingestion_run(db_session)
        doc = _make_document(db_session, run.id, "page.htm", "htm")
        CatalogerTask(db_session).run([doc])
        assert doc.structure_class == "semi-structured"

    def test_xml_semi_structured(self, db_session: Session) -> None:
        run = _make_ingestion_run(db_session)
        doc = _make_document(db_session, run.id, "data.xml", "xml")
        CatalogerTask(db_session).run([doc])
        assert doc.structure_class == "semi-structured"

    def test_msg_semi_structured(self, db_session: Session) -> None:
        run = _make_ingestion_run(db_session)
        doc = _make_document(db_session, run.id, "email.msg", "msg")
        CatalogerTask(db_session).run([doc])
        assert doc.structure_class == "semi-structured"

    def test_auto_process_true_for_all_known(self, db_session: Session) -> None:
        """Every known extension results in can_auto_process=True."""
        run = _make_ingestion_run(db_session)
        for ext in sorted(_ALL_KNOWN_EXTENSIONS):
            doc = _make_document(db_session, run.id, f"file.{ext}", ext)
            CatalogerTask(db_session).run([doc])
            assert doc.can_auto_process is True, f"Expected True for .{ext}"
            assert doc.manual_review_reason is None, f"Expected None for .{ext}"


# ===========================================================================
# Catalog-summary endpoint integration
# ===========================================================================


class TestCatalogSummaryIntegration:
    """Verify catalog-summary endpoint works with cataloger output."""

    @pytest.fixture()
    def api_session(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        session = SessionLocal()
        yield session
        session.close()

    @pytest.fixture()
    def client(self, api_session: Session, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")

        from app.core.settings import get_settings

        get_settings.cache_clear()

        from app.api.deps import get_db
        from app.api.main import app

        def _override():
            yield api_session

        app.dependency_overrides[get_db] = _override
        from fastapi.testclient import TestClient

        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
        app.dependency_overrides.clear()
        get_settings.cache_clear()

    def test_catalog_summary_reflects_cataloger(
        self, api_session: Session, client
    ) -> None:
        """Create a project, link documents via ingestion run, catalog,
        then verify the summary endpoint returns the correct counts."""
        from app.db.models import Project

        project = Project(id=uuid4(), name="Test Project")
        api_session.add(project)
        api_session.flush()

        run = IngestionRun(
            id=uuid4(),
            project_id=project.id,
            source_path="/tmp/test",
            config_hash="abc",
            code_version="1.0",
            initiated_by="test",
        )
        api_session.add(run)
        api_session.flush()

        docs = [
            _make_document(api_session, run.id, "a.csv", "csv"),
            _make_document(api_session, run.id, "b.pdf", "pdf"),
            _make_document(api_session, run.id, "c.dat", "dat"),
        ]
        CatalogerTask(api_session).run(docs)
        api_session.commit()

        resp = client.get(f"/projects/{project.id}/catalog-summary")
        assert resp.status_code == 200
        data = resp.json()

        assert data["total_documents"] == 3
        assert data["auto_processable"] == 2
        assert data["manual_review"] == 1
        assert data["by_structure_class"]["structured"] == 1
        assert data["by_structure_class"]["unstructured"] == 1
        assert data["by_structure_class"]["non-extractable"] == 1
