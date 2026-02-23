"""Reviewer role definitions and queue-role mappings — Phase 4.

Roles:
- REVIEWER: reviews AI-flagged records
- LEGAL_REVIEWER: regulatory judgment on escalated cases
- APPROVER: final sign-off before notification (can action any queue)
- QC_SAMPLER: random sampling validation
"""
from __future__ import annotations

ROLES = ["REVIEWER", "LEGAL_REVIEWER", "APPROVER", "QC_SAMPLER"]

VALID_ROLES: frozenset[str] = frozenset(ROLES)

QUEUE_ROLE_MAP: dict[str, str] = {
    "low_confidence": "REVIEWER",
    "escalation": "LEGAL_REVIEWER",
    "qc_sampling": "QC_SAMPLER",
    "rra_review": "REVIEWER",
}

VALID_QUEUE_TYPES: frozenset[str] = frozenset(QUEUE_ROLE_MAP.keys())


def required_role_for_queue(queue_type: str) -> str:
    """Return the role required to work items in *queue_type*."""
    if queue_type not in QUEUE_ROLE_MAP:
        raise ValueError(
            f"Unknown queue_type {queue_type!r}; "
            f"must be one of {sorted(VALID_QUEUE_TYPES)}"
        )
    return QUEUE_ROLE_MAP[queue_type]


def can_action_queue(role: str, queue_type: str) -> bool:
    """Return whether *role* is allowed to action items in *queue_type*.

    ``APPROVER`` can action any queue (override).
    """
    if role not in VALID_ROLES:
        raise ValueError(
            f"Unknown role {role!r}; must be one of {sorted(VALID_ROLES)}"
        )
    if queue_type not in VALID_QUEUE_TYPES:
        raise ValueError(
            f"Unknown queue_type {queue_type!r}; "
            f"must be one of {sorted(VALID_QUEUE_TYPES)}"
        )
    if role == "APPROVER":
        return True
    return QUEUE_ROLE_MAP[queue_type] == role
