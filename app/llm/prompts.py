"""Prompt templates for LLM-assisted PII classification.

Each template uses Python string ``.format()`` placeholders and instructs
the LLM to respond in structured JSON.  Templates MUST NOT contain raw PII --
callers are responsible for passing only masked / redacted values.

Use cases:
- Classify ambiguous entities whose deterministic layer confidence is low.
- Assess whether a low-confidence extraction is a true positive.
- Suggest which data categories (PII/SPII/PHI/PFI/PCI/NPI/FTI/CREDENTIALS)
  apply to a given entity type.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# System prompt shared by all LLM-assist calls
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a breach-notification classification assistant.  "
    "You ONLY output valid JSON.  No prose, no markdown fences, no commentary.  "
    "Your decisions must be explainable and conservative -- when in doubt, "
    "classify as PII and flag for human review."
)

# ---------------------------------------------------------------------------
# CLASSIFY_AMBIGUOUS_ENTITY
# ---------------------------------------------------------------------------

CLASSIFY_AMBIGUOUS_ENTITY = (
    "Given the following context window around a detected entity, classify the "
    "entity into one of the standard PII types.\n"
    "\n"
    "Context (surrounding text, PII values redacted):\n"
    "```\n"
    "{context_window}\n"
    "```\n"
    "\n"
    "Detected value (masked): {masked_value}\n"
    "Detection method: {detection_method}\n"
    "Current candidate type: {candidate_type}\n"
    "Confidence score: {confidence_score}\n"
    "\n"
    "Respond with a JSON object containing exactly these keys:\n"
    "  - \"entity_type\": the most likely PII type (e.g. US_SSN, EMAIL, PHONE_US, etc.)\n"
    "  - \"confidence\": your confidence in this classification (0.0 to 1.0)\n"
    "  - \"rationale\": a brief explanation of why you chose this type\n"
    "  - \"alternative_types\": a list of other plausible types (may be empty)\n"
    "\n"
    "Respond ONLY with valid JSON.  No additional text."
)

# ---------------------------------------------------------------------------
# ASSESS_EXTRACTION_CONFIDENCE
# ---------------------------------------------------------------------------

ASSESS_EXTRACTION_CONFIDENCE = (
    "An extraction pipeline flagged the following as potential PII but with "
    "low confidence.  Assess whether this is likely a true positive.\n"
    "\n"
    "Entity type: {entity_type}\n"
    "Masked value: {masked_value}\n"
    "Detection layer: {extraction_layer}\n"
    "Pattern used: {pattern_name}\n"
    "Original confidence: {original_confidence}\n"
    "Context (surrounding text, PII values redacted):\n"
    "```\n"
    "{context_window}\n"
    "```\n"
    "\n"
    "Respond with a JSON object containing exactly these keys:\n"
    "  - \"is_true_positive\": true or false\n"
    "  - \"adjusted_confidence\": your revised confidence (0.0 to 1.0)\n"
    "  - \"rationale\": a brief explanation\n"
    "  - \"recommend_human_review\": true or false\n"
    "\n"
    "Respond ONLY with valid JSON.  No additional text."
)

# ---------------------------------------------------------------------------
# SUGGEST_ENTITY_CATEGORY
# ---------------------------------------------------------------------------

SUGGEST_ENTITY_CATEGORY = (
    "Given the following PII entity type, suggest which data categories "
    "it belongs to.  Categories are: PII, SPII, PHI, PFI, PCI, NPI, FTI, "
    "CREDENTIALS.\n"
    "\n"
    "Entity type: {entity_type}\n"
    "Description: {entity_description}\n"
    "Current assigned categories: {current_categories}\n"
    "\n"
    "Respond with a JSON object containing exactly these keys:\n"
    "  - \"categories\": a list of applicable category codes (e.g. [\"PII\", \"SPII\"])\n"
    "  - \"rationale\": a brief explanation for each category assignment\n"
    "  - \"additional_categories\": any categories NOT in the current list "
    "that you think should be added (may be empty list)\n"
    "\n"
    "Respond ONLY with valid JSON.  No additional text."
)

# ---------------------------------------------------------------------------
# Template registry for programmatic access
# ---------------------------------------------------------------------------

PROMPT_TEMPLATES: dict[str, str] = {
    "classify_ambiguous_entity": CLASSIFY_AMBIGUOUS_ENTITY,
    "assess_extraction_confidence": ASSESS_EXTRACTION_CONFIDENCE,
    "suggest_entity_category": SUGGEST_ENTITY_CATEGORY,
}
