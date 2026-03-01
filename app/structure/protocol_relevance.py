"""Protocol → entity role relevance mapping.

Maps each protocol to a dict of entity roles and their relevance:
- ``"target"``: PII from this role is the primary notification target
- ``"deprioritize"``: PII is secondary; reduced confidence, still reviewed
- ``"non-target"``: PII is not relevant to this protocol (institutional noise)

When a protocol is not in the mapping, all roles default to ``"target"``
(conservative — never silently drop PII).
"""
from __future__ import annotations

from app.structure.models import EntityRole

# Relevance levels
TARGET = "target"
DEPRIORITIZE = "deprioritize"
NON_TARGET = "non-target"

VALID_RELEVANCE_LEVELS: frozenset[str] = frozenset({TARGET, DEPRIORITIZE, NON_TARGET})

# ---------------------------------------------------------------------------
# Protocol → role relevance
# ---------------------------------------------------------------------------

PROTOCOL_TARGET_ROLES: dict[str, dict[EntityRole, str]] = {
    "hipaa_breach_rule": {
        "primary_subject": TARGET,
        "secondary_contact": DEPRIORITIZE,
        "provider": NON_TARGET,
        "institutional": NON_TARGET,
        "unknown": TARGET,  # conservative default
    },
    "ferpa": {
        "primary_subject": TARGET,
        "secondary_contact": DEPRIORITIZE,
        "provider": NON_TARGET,
        "institutional": NON_TARGET,
        "unknown": TARGET,
    },
    "ccpa": {
        "primary_subject": TARGET,
        "secondary_contact": DEPRIORITIZE,
        "provider": NON_TARGET,
        "institutional": NON_TARGET,
        "unknown": TARGET,
    },
    "hitech": {
        "primary_subject": TARGET,
        "secondary_contact": DEPRIORITIZE,
        "provider": NON_TARGET,
        "institutional": NON_TARGET,
        "unknown": TARGET,
    },
    "state_breach_generic": {
        "primary_subject": TARGET,
        "secondary_contact": DEPRIORITIZE,
        "provider": NON_TARGET,
        "institutional": NON_TARGET,
        "unknown": TARGET,
    },
    "bipa": {
        "primary_subject": TARGET,
        "secondary_contact": DEPRIORITIZE,
        "provider": NON_TARGET,
        "institutional": NON_TARGET,
        "unknown": TARGET,
    },
    "dpdpa": {
        "primary_subject": TARGET,
        "secondary_contact": DEPRIORITIZE,
        "provider": NON_TARGET,
        "institutional": NON_TARGET,
        "unknown": TARGET,
    },
    "gdpr": {
        "primary_subject": TARGET,
        "secondary_contact": TARGET,  # GDPR protects all data subjects
        "provider": DEPRIORITIZE,
        "institutional": NON_TARGET,
        "unknown": TARGET,
    },
}


def get_role_relevance(protocol_id: str, entity_role: EntityRole) -> str:
    """Return the relevance level for a given protocol + entity role combination.

    Returns ``"target"`` if the protocol or role is unknown (conservative default).
    """
    protocol_map = PROTOCOL_TARGET_ROLES.get(protocol_id)
    if protocol_map is None:
        return TARGET  # unknown protocol → treat everything as target
    return protocol_map.get(entity_role, TARGET)
