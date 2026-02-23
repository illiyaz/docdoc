from __future__ import annotations

from typing import Generic, TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.policies import StoragePolicyConfig, build_extraction_storage
from app.core.security import SecurityService
from app.db import models

ModelT = TypeVar("ModelT")


class BaseRepository(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, db: Session):
        self.db = db

    def create(self, **kwargs) -> ModelT:
        entity = self.model(**kwargs)
        self.db.add(entity)
        self.db.flush()
        return entity

    def get(self, entity_id: UUID) -> ModelT | None:
        return self.db.get(self.model, entity_id)

    def list(self, limit: int = 100, offset: int = 0) -> list[ModelT]:
        stmt = select(self.model).offset(offset).limit(limit)
        return self.db.execute(stmt).scalars().all()

    def update(self, entity: ModelT, **kwargs) -> ModelT:
        for key, value in kwargs.items():
            setattr(entity, key, value)
        self.db.flush()
        return entity

    def delete(self, entity: ModelT) -> None:
        self.db.delete(entity)
        self.db.flush()


class IngestionRunRepository(BaseRepository[models.IngestionRun]):
    model = models.IngestionRun


class DocumentRepository(BaseRepository[models.Document]):
    model = models.Document


class ChunkRepository(BaseRepository[models.Chunk]):
    model = models.Chunk


class DetectionRepository(BaseRepository[models.Detection]):
    model = models.Detection


class ExtractionRepository(BaseRepository[models.Extraction]):
    model = models.Extraction

    def create_with_policy(
        self,
        *,
        raw_value: str,
        normalized_value: str | None,
        tenant_salt: str,
        security: SecurityService,
        policy_config: StoragePolicyConfig,
        **kwargs,
    ) -> models.Extraction:
        storage_fields = build_extraction_storage(
            raw_value=raw_value,
            normalized_value=normalized_value,
            tenant_salt=tenant_salt,
            security=security,
            config=policy_config,
        )
        return self.create(**kwargs, **storage_fields)


class PersonEntityRepository(BaseRepository[models.PersonEntity]):
    model = models.PersonEntity


class PersonLinkRepository(BaseRepository[models.PersonLink]):
    model = models.PersonLink


class ReviewTaskRepository(BaseRepository[models.ReviewTask]):
    model = models.ReviewTask


class ReviewDecisionRepository(BaseRepository[models.ReviewDecision]):
    model = models.ReviewDecision


class AuditEventRepository(BaseRepository[models.AuditEvent]):
    model = models.AuditEvent
