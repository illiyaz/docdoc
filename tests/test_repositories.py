from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cryptography.fernet import Fernet

from app.core.policies import StorageMode, StoragePolicyConfig, build_extraction_storage
from app.core.security import FernetEncryptionProvider, SecurityService
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    ChunkRepository,
    DetectionRepository,
    DocumentRepository,
    ExtractionRepository,
    IngestionRunRepository,
    PersonEntityRepository,
    PersonLinkRepository,
    ReviewDecisionRepository,
    ReviewTaskRepository,
)


def test_repository_crud_helpers_cover_all_entities():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with session_factory() as db:
        ingestion_run_repo = IngestionRunRepository(db)
        document_repo = DocumentRepository(db)
        chunk_repo = ChunkRepository(db)
        detection_repo = DetectionRepository(db)
        extraction_repo = ExtractionRepository(db)
        person_entity_repo = PersonEntityRepository(db)
        person_link_repo = PersonLinkRepository(db)
        review_task_repo = ReviewTaskRepository(db)
        review_decision_repo = ReviewDecisionRepository(db)
        audit_event_repo = AuditEventRepository(db)

        ingestion_run = ingestion_run_repo.create(
            source_path="/tmp/inbox",
            config_hash="c" * 64,
            code_version="main@1234567",
            initiated_by="system",
            mode="strict",
            status="pending",
        )
        document = document_repo.create(
            ingestion_run_id=ingestion_run.id,
            source_path="/tmp/inbox/a.pdf",
            file_name="a.pdf",
            file_type="pdf",
            sha256="a" * 64,
            status="discovered",
        )
        chunk = chunk_repo.create(document_id=document.id, chunk_index=0, text="john.doe@example.com")
        detection = detection_repo.create(
            document_id=document.id,
            chunk_id=chunk.id,
            detection_method="regex",
            pii_type="email",
            sensitivity="low",
        )
        extraction = extraction_repo.create(
            document_id=document.id,
            chunk_id=chunk.id,
            detection_id=detection.id,
            pii_type="email",
            sensitivity="low",
            hashed_value="b" * 64,
        )
        person_entity = person_entity_repo.create(ingestion_run_id=ingestion_run.id, entity_hash="c" * 64)
        person_link = person_link_repo.create(person_entity_id=person_entity.id, extraction_id=extraction.id)
        review_task = review_task_repo.create(queue_type="low_confidence", required_role="REVIEWER")
        review_decision = review_decision_repo.create(review_task_id=review_task.review_task_id, decision="approve")
        audit_event = audit_event_repo.create(event_type="ai_extraction")

        ingestion_run_repo.update(ingestion_run, status="running")
        document_repo.update(document, status="parsed")
        chunk_repo.update(chunk, is_boilerplate=True)
        detection_repo.update(detection, is_validated=True)
        extraction_repo.update(extraction, storage_policy="encrypted")
        person_entity_repo.update(person_entity, is_probabilistic=True)
        person_link_repo.update(person_link, is_primary=True)
        review_task_repo.update(review_task, status="COMPLETED")
        review_decision_repo.update(review_decision, rationale="validated")
        audit_event_repo.update(audit_event, decision="approved")

        db.commit()

        assert ingestion_run_repo.get(ingestion_run.id) is not None
        assert document_repo.get(document.id) is not None
        assert chunk_repo.get(chunk.id) is not None
        assert detection_repo.get(detection.id) is not None
        assert extraction_repo.get(extraction.id) is not None
        assert person_entity_repo.get(person_entity.id) is not None
        assert person_link_repo.get(person_link.id) is not None
        assert review_task_repo.get(review_task.review_task_id) is not None
        assert review_decision_repo.get(review_decision.id) is not None
        assert audit_event_repo.get(audit_event.audit_event_id) is not None
        assert len(document_repo.list()) == 1


def test_storage_policy_payloads_persist_expected_extraction_fields():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    security = SecurityService(FernetEncryptionProvider(Fernet.generate_key().decode("utf-8")))

    with session_factory() as db:
        ingestion_run_repo = IngestionRunRepository(db)
        document_repo = DocumentRepository(db)
        chunk_repo = ChunkRepository(db)
        extraction_repo = ExtractionRepository(db)

        ingestion_run = ingestion_run_repo.create(
            source_path="/tmp/inbox",
            config_hash="d" * 64,
            code_version="main@7654321",
            initiated_by="system",
            mode="strict",
            status="pending",
        )
        document = document_repo.create(
            ingestion_run_id=ingestion_run.id,
            source_path="/tmp/inbox/a.pdf",
            file_name="a.pdf",
            file_type="pdf",
            sha256="e" * 64,
            status="discovered",
        )
        chunk = chunk_repo.create(document_id=document.id, chunk_index=0, text="row")

        strict_extraction = extraction_repo.create_with_policy(
            raw_value="john.doe@example.com",
            normalized_value="john.doe@example.com",
            tenant_salt="tenant-1",
            security=security,
            policy_config=StoragePolicyConfig(mode=StorageMode.STRICT),
            document_id=document.id,
            chunk_id=chunk.id,
            pii_type="email",
            sensitivity="low",
        )

        investigation_payload = build_extraction_storage(
            raw_value="john.doe@example.com",
            normalized_value="john.doe@example.com",
            tenant_salt="tenant-1",
            security=security,
            config=StoragePolicyConfig(mode=StorageMode.INVESTIGATION, investigation_retention_days=7),
        )
        investigation_extraction = extraction_repo.create_with_policy(
            raw_value="john.doe@example.com",
            normalized_value="john.doe@example.com",
            tenant_salt="tenant-1",
            security=security,
            policy_config=StoragePolicyConfig(mode=StorageMode.INVESTIGATION, investigation_retention_days=7),
            document_id=document.id,
            chunk_id=chunk.id,
            pii_type="email",
            sensitivity="low",
        )

        db.commit()

        assert strict_extraction.raw_value_encrypted is None
        assert strict_extraction.retention_until is None
        assert strict_extraction.storage_policy == "hash"

        assert investigation_extraction.raw_value_encrypted is not None
        assert investigation_extraction.retention_until is not None
        assert investigation_extraction.storage_policy == "encrypted"
        assert security.decrypt(investigation_extraction.raw_value_encrypted) == "john.doe@example.com"
        assert security.decrypt(investigation_payload["raw_value_encrypted"]) == "john.doe@example.com"
