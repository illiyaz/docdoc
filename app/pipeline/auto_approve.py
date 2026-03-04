"""Auto-approve logic for two-phase pipeline document analysis review.

Determines whether a document's sample extractions are confident enough
to skip human review, based on protocol configuration.
"""
from __future__ import annotations


def should_auto_approve(
    sample_confidences: list[float],
    protocol_config: dict | None = None,
    base_protocol_id: str | None = None,
) -> tuple[bool, str]:
    """Determine if a document's analysis should be auto-approved.

    Parameters
    ----------
    sample_confidences:
        Confidence scores from sample PII detections on the onset page.
    protocol_config:
        Protocol configuration dict (may contain auto_approve settings).
    base_protocol_id:
        Base protocol ID (e.g., "hipaa_breach_rule").

    Returns
    -------
    (approved, reason) tuple.
    """
    config = protocol_config or {}
    auto_config = config.get("auto_approve", {})

    if not auto_config.get("enabled", True):
        return (False, "auto-approve disabled in protocol config")

    require_review_for = auto_config.get("require_review_for_protocols", [])
    if base_protocol_id and base_protocol_id in require_review_for:
        return (False, f"protocol '{base_protocol_id}' requires human review")

    if not sample_confidences:
        return (False, "no PII entities found in sample page")

    min_entities = auto_config.get("min_sample_entities", 3)
    if len(sample_confidences) < min_entities:
        return (False, f"only {len(sample_confidences)} entities found, need at least {min_entities}")

    avg_conf = sum(sample_confidences) / len(sample_confidences)
    min_confidence = auto_config.get("min_confidence", 0.85)

    if avg_conf >= min_confidence:
        return (True, f"auto-approved: avg confidence {avg_conf:.2f} >= {min_confidence}")

    return (False, f"avg confidence {avg_conf:.2f} below threshold {min_confidence}")
