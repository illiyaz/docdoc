"""Notification list builder — Phase 3.

Applies a ``Protocol`` to a set of ``NotificationSubject`` rows,
filters to those requiring notification, and persists the resulting
``NotificationList`` to the database.
"""
from __future__ import annotations

import logging
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.db.models import NotificationList, NotificationSubject
from app.protocols.protocol import Protocol
from app.protocols.regulatory_threshold import apply_protocol_to_all

logger = logging.getLogger(__name__)


def build_notification_list(
    job_id: str,
    protocol: Protocol,
    subjects: list[NotificationSubject],
    db_session: Session,
) -> NotificationList:
    """Apply *protocol* to all *subjects* and persist a ``NotificationList``.

    Steps:
    1. Apply protocol to every subject (updates ``notification_required``).
    2. Collect subject IDs where ``notification_required`` is True.
    3. Create a ``NotificationList`` row (status ``"EMPTY"`` when no
       subjects are triggered, ``"PENDING"`` otherwise).
    4. Flush (not commit) and return.
    """
    results = apply_protocol_to_all(subjects, protocol, db_session)

    triggered_ids = [
        sid for sid, required in results.items() if required
    ]

    status = "EMPTY" if not triggered_ids else "PENDING"

    nl = NotificationList(
        notification_list_id=uuid4(),
        job_id=job_id,
        protocol_id=protocol.protocol_id,
        subject_ids=triggered_ids,
        status=status,
    )
    db_session.add(nl)
    db_session.flush()
    return nl


def get_notification_subjects(
    notification_list: NotificationList,
    db_session: Session,
) -> list[NotificationSubject]:
    """Retrieve ``NotificationSubject`` objects for a ``NotificationList``.

    Returns subjects in the same order as ``notification_list.subject_ids``.
    Missing subjects are logged as warnings and skipped.
    """
    subject_ids = notification_list.subject_ids or []
    results: list[NotificationSubject] = []
    for sid in subject_ids:
        ns = db_session.get(NotificationSubject, UUID(sid) if isinstance(sid, str) else sid)
        if ns is None:
            logger.warning("NotificationSubject %s not found — skipping", sid)
            continue
        results.append(ns)
    return results
