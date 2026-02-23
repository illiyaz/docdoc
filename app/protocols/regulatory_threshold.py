"""Regulatory threshold evaluator — Phase 3.

Given a ``Protocol`` and a ``NotificationSubject``, determines whether
the subject's PII types meet the protocol's notification threshold and
updates the ``notification_required`` flag in the database.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import NotificationSubject
from app.protocols.protocol import Protocol


def apply_protocol(
    subject: NotificationSubject,
    protocol: Protocol,
) -> tuple[bool, list[str]]:
    """Check whether *subject*'s PII types trigger notification under *protocol*.

    Returns
    -------
    tuple[bool, list[str]]
        ``(notification_required, triggered_by)`` where *triggered_by* is
        the sorted list of entity types from the subject that matched the
        protocol's triggering types.  Empty list if not triggered.
    """
    subject_types = subject.pii_types_found or []
    triggers_upper = {t.upper() for t in protocol.triggering_entity_types}
    matched = sorted({t for t in subject_types if t.upper() in triggers_upper})
    required = len(matched) >= protocol.notification_threshold
    return required, matched if required else []


def apply_protocol_to_all(
    subjects: list[NotificationSubject],
    protocol: Protocol,
    db_session: Session,
) -> dict[str, bool]:
    """Apply *protocol* to every subject and persist the result.

    Updates ``notification_required`` on each subject in the database.
    The session is flushed but **not** committed — the caller owns the
    transaction.

    Returns
    -------
    dict[str, bool]
        Mapping of ``{subject_id: notification_required}``.
    """
    results: dict[str, bool] = {}
    for subject in subjects:
        required, _ = apply_protocol(subject, protocol)
        subject.notification_required = required
        results[str(subject.subject_id)] = required
    db_session.flush()
    return results
