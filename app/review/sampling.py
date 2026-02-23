"""QC sampling strategy — Phase 4.

Selects a statistically valid random sample of AI-approved records
for human QC validation (5–10% of approved output).
"""
from __future__ import annotations

import logging
import random
from math import ceil

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import NotificationSubject, ReviewTask
from app.review.queue_manager import QueueManager

logger = logging.getLogger(__name__)


class SamplingStrategy:
    """Select subjects for QC sampling and create review tasks."""

    def __init__(
        self,
        db_session: Session,
        sample_rate: float = 0.05,
        min_sample: int = 1,
        max_sample: int | None = None,
    ) -> None:
        if not (0.0 < sample_rate <= 1.0):
            raise ValueError(
                f"sample_rate must be in (0.0, 1.0]; got {sample_rate}"
            )
        if min_sample < 1:
            raise ValueError(
                f"min_sample must be >= 1; got {min_sample}"
            )
        self.db = db_session
        self.sample_rate = sample_rate
        self.min_sample = min_sample
        self.max_sample = max_sample

    def calculate_sample_size(self, population_size: int) -> int:
        """Return sample size for *population_size* (pure function)."""
        if population_size <= 0:
            return 0
        size = max(self.min_sample, ceil(population_size * self.sample_rate))
        if self.max_sample is not None:
            size = min(size, self.max_sample)
        return min(size, population_size)

    def generate_qc_sample(
        self,
        queue_manager: QueueManager,
    ) -> list[ReviewTask]:
        """Create ``qc_sampling`` review tasks for a random sample of AI_PENDING subjects."""
        stmt = (
            select(NotificationSubject)
            .where(NotificationSubject.review_status == "AI_PENDING")
            .order_by(NotificationSubject.created_at.asc())
        )
        subjects = list(self.db.execute(stmt).scalars().all())

        sample_size = self.calculate_sample_size(len(subjects))
        if sample_size == 0:
            return []

        sampled = random.sample(subjects, sample_size)

        tasks: list[ReviewTask] = []
        for subj in sampled:
            try:
                task = queue_manager.create_task("qc_sampling", str(subj.subject_id))
                tasks.append(task)
            except ValueError:
                logger.debug(
                    "Subject %s already has a qc_sampling task — skipped",
                    subj.subject_id,
                )
        return tasks
