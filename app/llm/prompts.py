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
# ANALYZE_DOCUMENT_STRUCTURE
# ---------------------------------------------------------------------------

ANALYZE_DOCUMENT_STRUCTURE = (
    "Analyze the structure of the following document excerpt.  Identify:\n"
    "1. The document type (medical_record, student_file, financial_statement, "
    "employment_record, insurance_document, legal_document, correspondence, "
    "form_fillable, or unknown)\n"
    "2. Sections within the document (patient_information, provider_information, "
    "emergency_contact, student_information, parent_guardian_information, "
    "school_information, employee_information, employer_information, "
    "account_holder_information, financial_institution, header_footer, "
    "legal_boilerplate, or unknown)\n"
    "3. For each block, the entity role: primary_subject, secondary_contact, "
    "institutional, provider, or unknown\n"
    "\n"
    "Document excerpt (PII values have been masked):\n"
    "```\n"
    "{document_excerpt}\n"
    "```\n"
    "\n"
    "Respond with a JSON object containing exactly these keys:\n"
    "  - \"document_type\": one of the document types listed above\n"
    "  - \"confidence\": your confidence in the document type (0.0 to 1.0)\n"
    "  - \"sections\": a list of objects with keys: section_type, page_start, "
    "page_end, block_indices (list of ints), confidence\n"
    "  - \"entity_roles\": a list of objects with keys: block_index (int), "
    "entity_role, confidence, section_type (optional)\n"
    "\n"
    "Respond ONLY with valid JSON.  No additional text."
)

# ---------------------------------------------------------------------------
# ANALYZE_ENTITY_RELATIONSHIPS
# ---------------------------------------------------------------------------

ANALYZE_ENTITY_RELATIONSHIPS = (
    "You are analyzing a breach dataset document to understand entity relationships. "
    "Given the document excerpt and detected PII items below, identify:\n"
    "1. Which PII items belong to the same person or entity\n"
    "2. The role of each entity (primary_subject, institutional, provider, secondary_contact)\n"
    "3. Relationships between entity groups (e.g. employed_by, patient_of)\n"
    "\n"
    "Document type: {document_type}\n"
    "Document structure: {structure_summary}\n"
    "\n"
    "Document excerpt (from onset page {onset_page}):\n"
    "```\n"
    "{document_excerpt}\n"
    "```\n"
    "\n"
    "Detected PII items on this page:\n"
    "{pii_detections}\n"
    "\n"
    "Respond with a JSON object containing exactly these keys:\n"
    "  - \"document_summary\": a brief summary of what this document contains (1-2 sentences)\n"
    "  - \"entity_groups\": a list of objects, each with:\n"
    "      - \"group_id\": a short ID like \"G1\", \"G2\"\n"
    "      - \"label\": a human-readable label (e.g. \"John Smith (Employee)\")\n"
    "      - \"role\": one of primary_subject, institutional, provider, secondary_contact, unknown\n"
    "      - \"confidence\": your confidence in this grouping (0.0 to 1.0)\n"
    "      - \"members\": list of objects with: pii_type, value_ref (the detected value), page\n"
    "      - \"rationale\": why these PII items belong together\n"
    "  - \"relationships\": a list of objects with: from_group (group_id), to_group (group_id), "
    "relationship_type (e.g. employed_by, patient_of, parent_of, emergency_contact_for), confidence\n"
    "  - \"estimated_unique_individuals\": integer count of unique people detected\n"
    "  - \"extraction_guidance\": brief instructions on how PII is organized in this document "
    "(e.g. \"Each page contains one employee record with name, SSN, and address\")\n"
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
    "analyze_document_structure": ANALYZE_DOCUMENT_STRUCTURE,
    "analyze_entity_relationships": ANALYZE_ENTITY_RELATIONSHIPS,
}
