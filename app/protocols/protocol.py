"""Protocol dataclass — Phase 3.

A Protocol defines what triggers a notification obligation for a given
engagement.  Protocols are selected once per job and never changed
mid-job.  Custom protocols live in ``config/protocols/*.yaml``.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Protocol:
    """Breach notification protocol configuration."""

    protocol_id: str
    name: str
    jurisdiction: str
    triggering_entity_types: list[str]
    notification_threshold: int
    notification_deadline_days: int
    required_notification_content: list[str]
    regulatory_framework: str

    # Optional extensions — not all protocols need these.
    individual_deadline_days: int | None = None
    requires_hhs_notification: bool = False
    extra: dict = field(default_factory=dict)

    def is_triggered_by(self, entity_types: list[str]) -> bool:
        """Return True if any of *entity_types* triggers this protocol.

        Comparison is case-insensitive.
        """
        if not entity_types:
            return False
        triggers_upper = {t.upper() for t in self.triggering_entity_types}
        return any(et.upper() in triggers_upper for et in entity_types)
