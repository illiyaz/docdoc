"""Density scoring task: compute PII density summaries per document and project.

Runs AFTER extraction completes.  Each document receives a per-document
``DensitySummary`` row, and one project-level summary (``document_id=NULL``)
aggregates across all documents in the project.

Summaries contain:
    total_entities   — count of extractions
    by_category      — entity types grouped into 8 categories (PII, SPII,
                       PHI, PFI, PCI, NPI, FTI, CREDENTIALS).  A single
                       entity may increment multiple category counters when
                       it maps to more than one category.
    by_type          — raw counts per entity type
    confidence       — aggregate confidence label (high / partial / low)
    confidence_notes — human-readable list of quality notes
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constants import get_entity_categories
from app.db.models import DensitySummary, Extraction

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Entity type → category mapping (pure function)
# ---------------------------------------------------------------------------


def classify_entity_type(entity_type: str) -> str:
    """Map an entity type string to its primary category.

    For backward compatibility this returns a single string.  The primary
    category is the first entry in the multi-category list returned by
    :func:`classify_entity_categories`.

    Parameters
    ----------
    entity_type:
        The Presidio entity type string (e.g. ``"US_SSN"``, ``"MRN"``).

    Returns
    -------
    str
        Primary category (e.g. ``"PII"``, ``"PHI"``, ``"PFI"``).
    """
    return classify_entity_categories(entity_type)[0]


def classify_entity_categories(entity_type: str) -> list[str]:
    """Map an entity type string to all applicable categories.

    A single entity type may belong to multiple categories.  For example,
    ``US_SSN`` maps to ``["PII", "SPII"]`` and ``CREDIT_CARD`` maps to
    ``["PFI", "PCI"]``.

    Parameters
    ----------
    entity_type:
        The Presidio entity type string.

    Returns
    -------
    list[str]
        One or more category codes.  Unmapped types default to ``["PII"]``.
    """
    return get_entity_categories(entity_type)


# ---------------------------------------------------------------------------
# Confidence aggregation (pure function)
# ---------------------------------------------------------------------------

@dataclass
class ConfidenceResult:
    """Aggregated confidence label and explanatory notes."""
    label: str           # "high", "partial", or "low"
    notes: list[str]


def compute_confidence(
    confidence_scores: list[float | None],
) -> ConfidenceResult:
    """Compute an aggregate confidence label from a list of per-extraction scores.

    Thresholds:
        - ``"high"``    if >80% of scores are >= 0.75
        - ``"low"``     if >30% of scores are < 0.50
        - ``"partial"`` otherwise

    Parameters
    ----------
    confidence_scores:
        Per-extraction confidence values.  ``None`` entries are treated as
        missing (counted toward notes but excluded from threshold calculations).

    Returns
    -------
    ConfidenceResult
    """
    if not confidence_scores:
        return ConfidenceResult(label="high", notes=["No extractions to score"])

    total = len(confidence_scores)
    valid_scores = [s for s in confidence_scores if s is not None]
    missing_count = total - len(valid_scores)

    notes: list[str] = []

    if missing_count > 0:
        notes.append(f"{missing_count} extraction(s) with no confidence score")

    if not valid_scores:
        return ConfidenceResult(label="partial", notes=notes or ["All scores missing"])

    high_count = sum(1 for s in valid_scores if s >= 0.75)
    low_count = sum(1 for s in valid_scores if s < 0.50)

    if low_count > 0:
        notes.append(f"{low_count} low-confidence extraction(s)")

    # Thresholds are based on all scores (valid only)
    valid_total = len(valid_scores)

    if high_count / valid_total > 0.80:
        label = "high"
    elif low_count / valid_total > 0.30:
        label = "low"
        notes.append("OCR quality issues likely")
    else:
        label = "partial"

    return ConfidenceResult(label=label, notes=notes)


# ---------------------------------------------------------------------------
# DensityTask
# ---------------------------------------------------------------------------

@dataclass
class ExtractionInput:
    """Lightweight extraction data for density computation.

    Avoids coupling to the ORM model so the pure-function logic
    can be tested with plain dataclass instances.
    """
    document_id: UUID
    pii_type: str
    confidence_score: float | None


def _compute_density(
    extractions: list[ExtractionInput],
) -> tuple[int, dict[str, int], dict[str, int], ConfidenceResult]:
    """Pure function: compute density metrics from a list of extraction inputs.

    Returns
    -------
    tuple
        (total_entities, by_category, by_type, confidence_result)
    """
    total_entities = len(extractions)
    by_type: dict[str, int] = {}
    by_category: dict[str, int] = {}

    for ext in extractions:
        # by_type
        pii_type = ext.pii_type or "UNKNOWN"
        by_type[pii_type] = by_type.get(pii_type, 0) + 1

        # by_category — a single entity may increment multiple categories
        categories = classify_entity_categories(pii_type)
        for category in categories:
            by_category[category] = by_category.get(category, 0) + 1

    scores = [ext.confidence_score for ext in extractions]
    confidence = compute_confidence(scores)

    return total_entities, by_category, by_type, confidence


class DensityTask:
    """Compute and persist PII density summaries for a project.

    Usage::

        task = DensityTask(db_session)
        summaries = task.run(project_id)

    Creates one ``DensitySummary`` row per document plus one project-level
    row (``document_id=NULL``).
    """

    def __init__(self, db_session: Session) -> None:
        self.db = db_session

    def run(
        self,
        project_id: UUID,
        extraction_inputs: list[ExtractionInput] | None = None,
    ) -> list[DensitySummary]:
        """Compute density summaries and persist them.

        Parameters
        ----------
        project_id:
            The project to compute density for.
        extraction_inputs:
            Optional pre-built list of ``ExtractionInput``.  If ``None``,
            extractions are queried from the DB via ingestion runs linked
            to the project.

        Returns
        -------
        list[DensitySummary]
            All created summary rows (per-document + project-level).
        """
        if extraction_inputs is None:
            extraction_inputs = self._load_extractions(project_id)

        # Group by document
        by_doc: dict[UUID, list[ExtractionInput]] = {}
        for ext in extraction_inputs:
            by_doc.setdefault(ext.document_id, []).append(ext)

        summaries: list[DensitySummary] = []

        # Per-document summaries
        for doc_id, doc_extractions in by_doc.items():
            total, by_cat, by_typ, conf = _compute_density(doc_extractions)
            ds = DensitySummary(
                project_id=project_id,
                document_id=doc_id,
                total_entities=total,
                by_category=by_cat,
                by_type=by_typ,
                confidence=conf.label,
                confidence_notes=json.dumps(conf.notes),
            )
            self.db.add(ds)
            summaries.append(ds)

        # Project-level summary (document_id=NULL)
        total, by_cat, by_typ, conf = _compute_density(extraction_inputs)
        project_ds = DensitySummary(
            project_id=project_id,
            document_id=None,
            total_entities=total,
            by_category=by_cat,
            by_type=by_typ,
            confidence=conf.label,
            confidence_notes=json.dumps(conf.notes),
        )
        self.db.add(project_ds)
        summaries.append(project_ds)

        self.db.flush()

        doc_count = len(by_doc)
        logger.info(
            "Density task complete: project=%s, %d document(s), %d total entities, confidence=%s",
            project_id,
            doc_count,
            total,
            conf.label,
        )

        return summaries

    # ---- internal ---------------------------------------------------------

    def _load_extractions(self, project_id: UUID) -> list[ExtractionInput]:
        """Query extractions from DB via ingestion runs linked to the project."""
        from app.db.models import Document, IngestionRun

        rows = self.db.execute(
            select(
                Extraction.document_id,
                Extraction.pii_type,
                Extraction.confidence_score,
            )
            .join(Document, Extraction.document_id == Document.id)
            .join(IngestionRun, Document.ingestion_run_id == IngestionRun.id)
            .where(IngestionRun.project_id == project_id)
        ).all()

        return [
            ExtractionInput(
                document_id=row.document_id,
                pii_type=row.pii_type,
                confidence_score=row.confidence_score,
            )
            for row in rows
        ]
