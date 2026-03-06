"""Event type constants — Phase 4.

Canonical event types for the append-only audit trail.
"""
from __future__ import annotations

EVENT_AI_EXTRACTION = "ai_extraction"
EVENT_HUMAN_REVIEW = "human_review"
EVENT_ESCALATION = "escalation"
EVENT_LEGAL_REVIEW = "legal_review"
EVENT_APPROVAL = "approval"
EVENT_NOTIFICATION_SENT = "notification_sent"
EVENT_PROTOCOL_APPLIED = "protocol_applied"
EVENT_RRA_MERGE = "rra_merge"
EVENT_DETECTION_SUPPRESSED = "detection_suppressed"
EVENT_DETECTION_RECLASSIFIED = "detection_reclassified"

VALID_EVENT_TYPES: frozenset[str] = frozenset({
    EVENT_AI_EXTRACTION,
    EVENT_HUMAN_REVIEW,
    EVENT_ESCALATION,
    EVENT_LEGAL_REVIEW,
    EVENT_APPROVAL,
    EVENT_NOTIFICATION_SENT,
    EVENT_PROTOCOL_APPLIED,
    EVENT_RRA_MERGE,
    EVENT_DETECTION_SUPPRESSED,
    EVENT_DETECTION_RECLASSIFIED,
})
