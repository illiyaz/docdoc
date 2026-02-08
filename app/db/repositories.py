from __future__ import annotations

from typing import Generic, TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

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
