"""NotificationSubject builder — Phase 2.

Constructs a ``NotificationSubject`` ORM record from the output of the
entity resolver and deduplicator.  Populates canonical contact fields
using the normalization package and assembles the ``source_records``
provenance list from the originating ``Extraction`` rows.
"""
from __future__ import annotations


def build_notification_subject(resolved_group: list) -> object:
    """Build a ``NotificationSubject`` from a resolved extraction group.

    Parameters
    ----------
    resolved_group:
        List of ``Extraction`` ORM objects that have been resolved to
        the same individual by the entity resolver.

    Returns
    -------
    NotificationSubject
        Unsaved ORM instance ready for ``session.add()``.

    Raises
    ------
    NotImplementedError
        Phase 2 — not yet implemented.
    """
    raise NotImplementedError(
        "notification_subject.build_notification_subject is not yet implemented (Phase 2)"
    )
