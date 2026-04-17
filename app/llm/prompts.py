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
# UNDERSTAND_DOCUMENT (Phase 14b — LLM Document Understanding)
# ---------------------------------------------------------------------------

UNDERSTAND_DOCUMENT = (
    "You are analyzing a document to understand its structure and identify what "
    "data fields mean.\n"
    "\n"
    "Document: {file_name} ({file_type}, {structure_class})\n"
    "Heuristic analysis suggests: {heuristic_doc_type}\n"
    "\n"
    "--- DOCUMENT TEXT (page {onset_page}) ---\n"
    "{page_text}\n"
    "--- END ---\n"
    "\n"
    "Analyze this document and respond ONLY with a JSON object:\n"
    '{{\n'
    '  "document_type": "the type of document (financial_statement, medical_record, '
    'hr_file, insurance_claim, legal_filing, tax_form, correspondence, etc.)",\n'
    '  "document_subtype": "more specific type if identifiable",\n'
    '  "issuing_entity": "the organization that produced this document, or null",\n'
    '  "field_map": [\n'
    '    {{\n'
    '      "label": "the field label as it appears in the document",\n'
    '      "value_example": "the value next to this label",\n'
    '      "semantic_type": "what this field actually represents (tax_id, account_number, '
    'reference_number, phone_number, address, etc.)",\n'
    '      "is_pii": true,\n'
    '      "presidio_override": "if is_pii, what Presidio entity type this should be '
    'classified as, else null",\n'
    '      "suppress_types": ["list of Presidio entity types that should NOT match this value"]\n'
    '    }}\n'
    '  ],\n'
    '  "people": [\n'
    '    {{\n'
    '      "name": "person name",\n'
    '      "role": "primary_subject | related_party | institutional_contact | provider",\n'
    '      "context": "how this person relates to the document",\n'
    '      "is_pii_subject": true\n'
    '    }}\n'
    '  ],\n'
    '  "organizations": ["list of organizations mentioned"],\n'
    '  "date_contexts": [\n'
    '    {{\n'
    '      "value": "the date as it appears",\n'
    '      "semantic_type": "transaction_date | statement_period | date_of_birth | '
    'filing_date | etc.",\n'
    '      "is_pii": false\n'
    '    }}\n'
    '  ],\n'
    '  "tables": [\n'
    '    {{\n'
    '      "columns": [\n'
    '        {{\n'
    '          "header": "the column header text",\n'
    '          "semantic_type": "what this column contains (transaction_date, reference_number, '
    'person_name, government_id, currency_amount, description_text, etc.)",\n'
    '          "contains_pii": false,\n'
    '          "pii_type": null\n'
    '        }}\n'
    '      ],\n'
    '      "row_count_estimate": 0,\n'
    '      "table_context": "what this table represents",\n'
    '      "has_pii_columns": false\n'
    '    }}\n'
    '  ],\n'
    '  "suppression_hints": ["free text hints about values that look like PII but are not"],\n'
    '  "extraction_notes": "brief note about what PII to expect and how it is organized",\n'
    '  "schema_confidence": 0.85,\n'
    '  "is_tabular": false,\n'
    '  "records_per_page_estimate": 1,\n'
    '  "layout_type": "variable",\n'
    '  "layout_confidence": 0.0,\n'
    '  "layout_field_map": null\n'
    '}}\n'
    "\n"
    "IMPORTANT:\n"
    "- Be precise about what IS and ISN'T PII. Reference numbers, account IDs, and "
    "statement numbers are NOT PII.\n"
    "- Phone/fax numbers belonging to organizations (not individuals) should be marked "
    "is_pii=false.\n"
    "- Dates that are transaction dates, statement periods, or filing dates are NOT "
    "dates of birth.\n"
    "- Short numeric codes (under 8 digits) next to labels like \"Client:\", \"Ref:\", "
    "\"Statement Nr:\" are reference numbers, NOT government IDs.\n"
    "- For tables: identify EVERY table on the page. Mark each column as contains_pii or "
    "not. Transaction tables (date, ref, description, amount) typically have ZERO PII "
    "columns. Payroll/HR tables (name, SSN, DOB, salary) have MULTIPLE PII columns.\n"
    "- A table column containing amounts, reference numbers, descriptions, or status "
    "values is NOT a PII column even if Presidio would match patterns in the data.\n"
    "- Set is_tabular=true if this document is a TABLE or LIST with MULTIPLE "
    "individuals per page (e.g., student roster, employee list, patient log). "
    "Set records_per_page_estimate to the approximate number of people per page.\n"
    "- EDUCATIONAL AND HR DOCUMENTS: School grade reports, student records, "
    "report cards, payroll stubs, employee rosters, and similar documents "
    "contain PII even if they only have names and addresses (no SSN, no "
    "government ID, no email). Under FERPA, student name + parent name + "
    "address is personally identifiable information. Under state breach laws, "
    "employee name + address from payroll/HR records is PII. These documents "
    "MUST be treated as PII-bearing and MUST receive layout_field_map entries "
    "for PERSON and LOCATION fields when they have a fixed or repeating layout.\n"
    "\n"
    "DOCUMENT STRUCTURE CLASSIFICATION:\n"
    "Identify which structural pattern this document follows:\n"
    "\n"
    "A) ONE-PERSON-PER-PAGE with labeled fields (pay stubs, tax forms, bank "
    "statements, EOBs, benefit confirmations, royalty statements, lab results). "
    "Set layout_type='fixed', records_per_page_estimate=1, is_tabular=false. "
    "Even if the page mentions other people (dependents, joint holders, providers), "
    "there is ONE primary subject per page. Use entity_role to distinguish.\n"
    "\n"
    "B) DELIMITED BLOCKS — multiple records per page separated by lines, "
    "whitespace, dashes ('----'), or repeating headers. Examples: payroll "
    "registers, HR rosters, client account summaries, patient visit logs. "
    "Set is_tabular=true, records_per_page_estimate=count of blocks per page. "
    "CRITICAL: identify the separator pattern in extraction_notes (e.g., "
    "'Records separated by dashed lines' or 'Each record starts with employee "
    "name in bold'). If records span across page breaks (e.g., 'CONTINUED' at "
    "bottom), note this: pages_per_instance may be >1 for split records.\n"
    "\n"
    "C) TRUE TABLES — column headers with one row per person. Examples: "
    "student enrollment lists, employee directories, voter rolls, mailing "
    "lists, delinquent account lists. Set is_tabular=true. IMPORTANT for "
    "tables: in the field_map, identify which column contains the SUBJECT "
    "NAME (entity_role=primary_subject) vs administrative data. If the table "
    "has sub-rows (e.g., student name row followed by course request rows), "
    "note this in extraction_notes. Multi-column layouts (side-by-side "
    "tables separated by pipes or whitespace) should set records_per_page "
    "to total records across ALL columns.\n"
    "\n"
    "D) MULTI-PERSON POSITIONAL — multiple people on the same page "
    "distinguished ONLY by spatial position, no explicit separators. "
    "Examples: school grade reports (student + parent + teacher at different "
    "y-positions), family benefit summaries (primary + spouse + children), "
    "household correspondence, joint tax filings, group insurance certs. "
    "Set layout_type='fixed', records_per_page_estimate=1 (one PRIMARY "
    "subject per page). Use entity_role on EACH field mapping to distinguish "
    "the primary subject from supporting people. The key insight: a page "
    "with 8 names does NOT mean 8 subjects — it means 1 subject with 7 "
    "supporting names (parents, teachers, providers, dependents). Only the "
    "primary subject and their guardian/dependents are notification targets.\n"
    "\n"
    "MASKING NOTE: The document text below has been pre-processed for safety. "
    "Certain values are replaced with masked placeholders: [PHONE] = phone number, "
    "[SSN] = Social Security Number, [EMAIL] = email address, [CREDIT_CARD] = credit "
    "card number. These placeholders represent REAL values that exist in the document. "
    "When you see [PHONE] appearing in the SAME position on EVERY page (e.g. in a "
    "header), that masked phone number is FIXED institutional text and is a VALID "
    "anchor for the layout_field_map. Use the placeholder itself (e.g. \"[PHONE]\") "
    "as the anchor_text value.\n"
    "\n"
    "LAYOUT ANALYSIS:\n"
    "- layout_type: Is every page formatted IDENTICALLY with labeled fields at fixed "
    "positions (\"fixed\"), does it follow a repeating template with slight variations "
    "(\"template_with_drift\"), or is the content freeform (\"variable\")?\n"
    "- layout_confidence: your confidence in the layout_type classification (0.0 to 1.0)\n"
    "- If layout_type is \"fixed\" or \"template_with_drift\", provide layout_field_map:\n"
    "  a list of coordinate-based field mappings for PII extraction:\n"
    "  [\n"
    "    {{\n"
    "      \"field_type\": \"MUST be one of: PERSON, LOCATION, DATE_OF_BIRTH, US_SSN, "
    "US_EIN, NI_NUMBER, AADHAAR, PAN_CARD, EMAIL_ADDRESS, PHONE_NUMBER, CREDIT_CARD, "
    "US_DRIVER_LICENSE, US_PASSPORT, IBAN_CODE, US_BANK_NUMBER, MEDICAL_LICENSE. "
    "Do NOT use domain-specific names like CLIENT, TAX_NO, EMPLOYEE_NAME. "
    "Map to the closest standard type. Use US_SSN for individual tax IDs "
    "(XXX-XX-XXXX format) and US_EIN for employer IDs (XX-XXXXXXX format).\",\n"
    "      \"anchor_text\": \"FIXED text that appears on EVERY page in the same position. "
    "This must be a label, heading, or institutional text that repeats identically — "
    "NEVER an actual data value (person name, address, etc.). Good anchors: 'Client:', "
    "'Tax No', 'Student Name:', '[PHONE]' (a masked institutional phone number), or an "
    "organization name. Bad anchors: 'John Smith', '123 Main St' (these are values, "
    "not labels).\",\n"
    "      \"spatial_relationship\": \"same_line_right | line_below | lines_below_N | region_right\",\n"
    "      \"value_pattern\": \"optional regex for validation (e.g. '\\\\d{{3}}-\\\\d{{2}}-\\\\d{{4}}' for SSN, "
    "'\\\\d{{2}}-\\\\d{{7}}' for EIN, or '\\\\d{{2,3}}-\\\\d{{2,7}}' for either)\",\n"
    "      \"sample_bbox\": [x0, y0, x1, y1],\n"
    "      \"line_count\": 1,\n"
    "      \"skip_pattern\": \"optional regex for text between label and value to skip\",\n"
    "      \"entity_role\": \"primary_subject | guardian | institutional | provider\"\n"
    "    }}\n"
    "  ]\n"
    "- entity_role is CRITICAL for correct extraction:\n"
    "  * primary_subject: the individual whose record this is (student, patient, employee, client)\n"
    "  * guardian: parent/legal guardian (extract but tag separately)\n"
    "  * institutional: school, hospital, employer (DO NOT create field mappings for these)\n"
    "  * provider: teacher, doctor, case worker (DO NOT create field mappings for these)\n"
    "  ONLY create layout_field_map entries for primary_subject and guardian fields.\n"
    "  In a school grade report: STUDENT NAME = primary_subject, PARENT NAMES = guardian, "
    "STUDENT ADDRESS = primary_subject. Teacher names in grades = provider (do NOT map).\n"
    "- Accounting statements, payslips, and labeled forms with identical layout per page "
    "are typically \"fixed\".\n"
    "- School grade reports, student transcripts, report cards, payroll stubs, and "
    "employee records with identical layout per page are also \"fixed\". A field map "
    "with ONLY PERSON and LOCATION fields is valid and expected for educational and "
    "HR documents — do NOT require SSN, email, or other government ID fields to "
    "justify creating a layout_field_map.\n"
    "- LABEL-LESS DOCUMENTS: Some fixed-layout documents have NO explicit labels like "
    "'Name:' or 'Address:'. Instead, the PII values always appear at the same LINE "
    "POSITION relative to fixed institutional text (school name, company header, phone "
    "number). In these cases, use the nearest FIXED text as the anchor. Example: a "
    "school grade report where every page starts with the school header, then "
    "[PHONE] (masked institutional phone), then parent name on the next line, student "
    "name below that, and address below that. The correct field map uses [PHONE] as anchor:\n"
    "  [{{\"field_type\": \"PERSON\", \"anchor_text\": \"[PHONE]\", "
    "\"spatial_relationship\": \"line_below\", \"line_count\": 1}}, "
    "{{\"field_type\": \"PERSON\", \"anchor_text\": \"[PHONE]\", "
    "\"spatial_relationship\": \"lines_below_2\", \"line_count\": 1}}, "
    "{{\"field_type\": \"LOCATION\", \"anchor_text\": \"[PHONE]\", "
    "\"spatial_relationship\": \"lines_below_3\", \"line_count\": 2}}]\n"
    "- SUPPRESSION: Organization phone numbers, ZIP codes, and institutional addresses "
    "that appear in the HEADER of every page are NOT personal PII. Do NOT map them as "
    "PII fields. A school's phone number is institutional, not personal. Similarly, "
    "'S1' or 'S2' next to grades means 'Semester 1/2', not a driver's license.\n"
    "- SPATIAL RELATIONSHIP — examine each field carefully:\n"
    "  * same_line_right: the value appears on the SAME LINE as the label, to its right, "
    "typically after a colon or spaces. Example: 'Tax No. : 285-07-5085' → same_line_right. "
    "'Client : John Smith' → same_line_right. If the colon and value are on the same "
    "line as the label text, use same_line_right.\n"
    "  * line_below: the value appears on the NEXT LINE below the label, with NO value "
    "on the same line as the label. Example: 'Address:\\n123 Main St' → line_below.\n"
    "  * lines_below_N: multi-line value starting on the next line. "
    "Example: full address spanning 4 lines → lines_below_4.\n"
    "  * region_right: multi-line block to the right of the anchor.\n"
    "- IMPORTANT: A single anchor can map to MULTIPLE fields.  For example, if "
    "\"In Account with :\" is followed by a name on the SAME LINE and an address "
    "on the LINES BELOW, create TWO field mappings: "
    "{{\"field_type\": \"PERSON\", \"anchor_text\": \"In Account with\", \"spatial_relationship\": \"same_line_right\", \"line_count\": 1}} "
    "AND {{\"field_type\": \"LOCATION\", \"anchor_text\": \"In Account with\", \"spatial_relationship\": \"lines_below_4\", \"line_count\": 4}}.  "
    "Always check if address/location content appears below a name label.\n"
    "- If layout_type is \"variable\", set layout_field_map to null.\n"
    "\n"
    "Respond ONLY with valid JSON.  No additional text."
)

# ---------------------------------------------------------------------------
# UNDERSTAND_MULTI_PAGE_DOCUMENT (Step 17 — template detection)
# ---------------------------------------------------------------------------

UNDERSTAND_MULTI_PAGE_DOCUMENT = (
    "You are analyzing a multi-page document to understand its structure.\n"
    "\n"
    "Document: {file_name} ({file_type}, {total_pages} pages)\n"
    "Protocol: {protocol_name}\n"
    "\n"
    "{pages_text}\n"
    "\n"
    "Analyze this document and determine:\n"
    "\n"
    "1. Is this a REPEATING TEMPLATE document where the same form repeats\n"
    "   for multiple individuals? If yes, identify:\n"
    "   - How many pages per individual (IMPORTANT: if each page is a COMPLETE\n"
    "     record for ONE individual and the next page is a DIFFERENT individual\n"
    "     with the SAME layout, then pages_per_instance = 1. Only set it > 1\n"
    "     when a SINGLE individual's information SPANS multiple consecutive\n"
    "     pages, e.g., page 1 = personal details, page 2 = medical history,\n"
    "     page 3 = insurance info, ALL for the SAME person.)\n"
    "   - Which page within the template has the person's name\n"
    "   - What PII fields appear on each page\n"
    "\n"
    "2. Is this a TABULAR document with MULTIPLE individuals per page\n"
    "   (e.g., student roster, employee list, patient log)?\n"
    "   If yes, set is_tabular=true and records_per_page_estimate.\n"
    "\n"
    "3. For each page, identify fields and their types.\n"
    "\n"
    "Respond ONLY with JSON:\n"
    '{{\n'
    '  "document_type": "the type of document",\n'
    '  "document_subtype": "more specific type",\n'
    '  "issuing_entity": "organization or null",\n'
    '  "template": {{\n'
    '    "is_repeating": true,\n'
    '    "template_name": "descriptive name for the repeating form",\n'
    '    "pages_per_instance": 1,\n'
    '    "total_instances_estimate": 5,\n'
    '    "instance_marker": "exact heading text that appears on the FIRST page of each individual, e.g. SUMMARY OF DETAILS IN RESPECT OF",\n'
    '    "page_roles": [\n'
    '      {{\n'
    '        "page_offset": 0,\n'
    '        "role": "what this page contains",\n'
    '        "pii_fields_expected": ["PERSON", "LOCATION"],\n'
    '        "is_identity_page": true\n'
    '      }}\n'
    '    ]\n'
    '  }},\n'
    '  "field_map": [],\n'
    '  "people": [],\n'
    '  "organizations": [],\n'
    '  "date_contexts": [],\n'
    '  "tables": [],\n'
    '  "suppression_hints": [],\n'
    '  "extraction_notes": "brief notes",\n'
    '  "schema_confidence": 0.85,\n'
    '  "is_tabular": false,\n'
    '  "records_per_page_estimate": 1,\n'
    '  "layout_type": "variable",\n'
    '  "layout_confidence": 0.0,\n'
    '  "layout_field_map": null\n'
    '}}\n'
    "\n"
    "IMPORTANT:\n"
    "- Financial terms (Lump Sum, Transfer Value, Premium, Pension, Annuity, "
    "Retirement, Benefit, Contribution, Entitlement, Remuneration) are NOT person names.\n"
    "- Location names (Harrow Weald, Buckman, Gallick) should be LOCATION, not PERSON.\n"
    "- If a value contains both a name and an ID number concatenated (e.g., \"Cole WI726762D\"), "
    "these are SEPARATE entities that should be extracted independently.\n"
    "- For repeating templates, describe the PATTERN (pages_per_instance), not every instance.\n"
    "- pages_per_instance MEANS: how many pages does ONE individual's data occupy?\n"
    "  If page 1 = Student A's report, page 2 = Student B's report (same layout),\n"
    "  then pages_per_instance = 1, NOT 2. Count pages for ONE person only.\n"
    "  The most common value is 1 (one page per person). Only use 2+ when a\n"
    "  single person's information genuinely continues across multiple pages.\n"
    "- If the document is NOT a repeating template, set template to null.\n"
    "- Be precise about what IS and ISN'T PII.\n"
    "- EDUCATIONAL AND HR DOCUMENTS: School grade reports, student records, "
    "report cards, payroll stubs, employee rosters, and similar documents "
    "contain PII even if they only have names and addresses (no SSN, no "
    "government ID, no email). Under FERPA, student name + parent name + "
    "address is personally identifiable information. Under state breach laws, "
    "employee name + address from payroll/HR records is PII. These documents "
    "MUST be treated as PII-bearing and MUST receive layout_field_map entries "
    "for PERSON and LOCATION fields when they have a fixed or repeating layout.\n"
    "\n"
    "MASKING NOTE: The document text below has been pre-processed for safety. "
    "Certain values are replaced with masked placeholders: [PHONE] = phone number, "
    "[SSN] = Social Security Number, [EMAIL] = email address, [CREDIT_CARD] = credit "
    "card number. These placeholders represent REAL values that exist in the document. "
    "When you see [PHONE] appearing in the SAME position on EVERY page (e.g. in a "
    "header), that masked phone number is FIXED institutional text and is a VALID "
    "anchor for the layout_field_map. Use the placeholder itself (e.g. \"[PHONE]\") "
    "as the anchor_text value.\n"
    "\n"
    "LAYOUT ANALYSIS:\n"
    "- layout_type: Is every page formatted IDENTICALLY with labeled fields at fixed "
    "positions (\"fixed\"), does it follow a repeating template with slight variations "
    "(\"template_with_drift\"), or is the content freeform (\"variable\")?\n"
    "- layout_confidence: your confidence in the layout_type classification (0.0 to 1.0)\n"
    "- If layout_type is \"fixed\" or \"template_with_drift\", provide layout_field_map:\n"
    "  a list of coordinate-based field mappings for PII extraction:\n"
    "  [\n"
    "    {{\n"
    "      \"field_type\": \"MUST be one of: PERSON, LOCATION, DATE_OF_BIRTH, US_SSN, "
    "NI_NUMBER, AADHAAR, PAN_CARD, EMAIL_ADDRESS, PHONE_NUMBER, CREDIT_CARD, "
    "US_DRIVER_LICENSE, US_PASSPORT, IBAN_CODE, US_BANK_NUMBER, MEDICAL_LICENSE. "
    "Do NOT use domain-specific names like CLIENT, TAX_NO, EMPLOYEE_NAME. "
    "Map to the closest standard type.\",\n"
    "      \"anchor_text\": \"FIXED text that appears on EVERY page in the same position. "
    "This must be a label, heading, or institutional text that repeats identically — "
    "NEVER an actual data value (person name, address, etc.). Good anchors: 'Client:', "
    "'Tax No', 'Student Name:', '[PHONE]' (a masked institutional phone number), or an "
    "organization name. Bad anchors: 'John Smith', '123 Main St' (these are values, "
    "not labels).\",\n"
    "      \"spatial_relationship\": \"same_line_right | line_below | lines_below_N | region_right\",\n"
    "      \"value_pattern\": \"optional regex for validation (e.g. '\\\\d{{3}}-\\\\d{{2}}-\\\\d{{4}}')\",\n"
    "      \"sample_bbox\": [x0, y0, x1, y1],\n"
    "      \"line_count\": 1,\n"
    "      \"skip_pattern\": \"optional regex for text between label and value to skip\",\n"
    "      \"entity_role\": \"primary_subject | guardian | institutional | provider\"\n"
    "    }}\n"
    "  ]\n"
    "- entity_role is CRITICAL for correct extraction:\n"
    "  * primary_subject: the individual whose record this is (student, patient, employee, client)\n"
    "  * guardian: parent/legal guardian (extract but tag separately)\n"
    "  * institutional: school, hospital, employer (DO NOT create field mappings for these)\n"
    "  * provider: teacher, doctor, case worker (DO NOT create field mappings for these)\n"
    "  ONLY create layout_field_map entries for primary_subject and guardian fields.\n"
    "  In a school grade report: STUDENT NAME = primary_subject, PARENT NAMES = guardian, "
    "STUDENT ADDRESS = primary_subject. Teacher names in grades = provider (do NOT map).\n"
    "- Accounting statements, payslips, and labeled forms with identical layout per page "
    "are typically \"fixed\".\n"
    "- School grade reports, student transcripts, report cards, payroll stubs, and "
    "employee records with identical layout per page are also \"fixed\". A field map "
    "with ONLY PERSON and LOCATION fields is valid and expected for educational and "
    "HR documents — do NOT require SSN, email, or other government ID fields to "
    "justify creating a layout_field_map.\n"
    "- LABEL-LESS DOCUMENTS: Some fixed-layout documents have NO explicit labels like "
    "'Name:' or 'Address:'. Instead, the PII values always appear at the same LINE "
    "POSITION relative to fixed institutional text (school name, company header, phone "
    "number). In these cases, use the nearest FIXED text as the anchor. Example: a "
    "school grade report where every page starts with the school header, then "
    "[PHONE] (masked institutional phone), then parent name on the next line, student "
    "name below that, and address below that. The correct field map uses [PHONE] as anchor:\n"
    "  [{{\"field_type\": \"PERSON\", \"anchor_text\": \"[PHONE]\", "
    "\"spatial_relationship\": \"line_below\", \"line_count\": 1}}, "
    "{{\"field_type\": \"PERSON\", \"anchor_text\": \"[PHONE]\", "
    "\"spatial_relationship\": \"lines_below_2\", \"line_count\": 1}}, "
    "{{\"field_type\": \"LOCATION\", \"anchor_text\": \"[PHONE]\", "
    "\"spatial_relationship\": \"lines_below_3\", \"line_count\": 2}}]\n"
    "- SUPPRESSION: Organization phone numbers, ZIP codes, and institutional addresses "
    "that appear in the HEADER of every page are NOT personal PII. Do NOT map them as "
    "PII fields. A school's phone number is institutional, not personal. Similarly, "
    "'S1' or 'S2' next to grades means 'Semester 1/2', not a driver's license.\n"
    "- A repeating template (template_with_drift) with a field map means EACH instance "
    "has the same labeled fields at the same positions.\n"
    "- IMPORTANT: A single anchor can map to MULTIPLE fields.  For example, if "
    "\"In Account with :\" is followed by a name on the SAME LINE and an address "
    "on the LINES BELOW, create TWO field mappings: "
    "{{\"field_type\": \"PERSON\", \"anchor_text\": \"In Account with\", \"spatial_relationship\": \"same_line_right\", \"line_count\": 1}} "
    "AND {{\"field_type\": \"LOCATION\", \"anchor_text\": \"In Account with\", \"spatial_relationship\": \"lines_below_4\", \"line_count\": 4}}.  "
    "Always check if address/location content appears below a name label.\n"
    "- If layout_type is \"variable\", set layout_field_map to null.\n"
    "\n"
    "Respond ONLY with valid JSON.  No additional text."
)

# ---------------------------------------------------------------------------
# UNDERSTAND_DOCUMENT_VISION (Vision fallback for docs with no text blocks)
# ---------------------------------------------------------------------------

UNDERSTAND_DOCUMENT_VISION = (
    "You are analyzing a document image to understand its structure and identify what "
    "data fields mean.  The document text could not be extracted digitally, so you must "
    "read the visible content from the image.\n"
    "\n"
    "Document: {file_name} ({file_type})\n"
    "\n"
    "Look at the document image and respond ONLY with a JSON object:\n"
    '{{\n'
    '  "document_type": "the type of document (financial_statement, medical_record, '
    'hr_file, insurance_claim, legal_filing, tax_form, correspondence, etc.)",\n'
    '  "document_subtype": "more specific type if identifiable",\n'
    '  "issuing_entity": "the organization that produced this document, or null",\n'
    '  "field_map": [\n'
    '    {{\n'
    '      "label": "the field label as it appears in the document",\n'
    '      "value_example": "the value next to this label",\n'
    '      "semantic_type": "what this field actually represents",\n'
    '      "is_pii": true,\n'
    '      "presidio_override": "Presidio entity type or null",\n'
    '      "suppress_types": []\n'
    '    }}\n'
    '  ],\n'
    '  "people": [\n'
    '    {{\n'
    '      "name": "person name visible in the document",\n'
    '      "role": "primary_subject | related_party | institutional_contact | provider",\n'
    '      "context": "how this person relates to the document",\n'
    '      "is_pii_subject": true\n'
    '    }}\n'
    '  ],\n'
    '  "organizations": ["list of organizations visible"],\n'
    '  "date_contexts": [\n'
    '    {{\n'
    '      "value": "the date as it appears",\n'
    '      "semantic_type": "transaction_date | date_of_birth | filing_date | etc.",\n'
    '      "is_pii": false\n'
    '    }}\n'
    '  ],\n'
    '  "tables": [],\n'
    '  "suppression_hints": [],\n'
    '  "extraction_notes": "brief note about what PII is visible and how it is organized",\n'
    '  "schema_confidence": 0.7,\n'
    '  "is_tabular": false,\n'
    '  "records_per_page_estimate": 1,\n'
    '  "layout_type": "variable",\n'
    '  "layout_confidence": 0.0,\n'
    '  "layout_field_map": null\n'
    '}}\n'
    "\n"
    "IMPORTANT:\n"
    "- Read ALL text visible in the image carefully.\n"
    "- Report EXACT values as you see them — do not guess or infer.\n"
    "- Be precise about what IS and ISN'T PII.\n"
    "- If you cannot read the document clearly, set schema_confidence low.\n"
    "\n"
    "Respond ONLY with valid JSON.  No additional text."
)

# ---------------------------------------------------------------------------
# SEGREGATION — LLM-first file classification (Step 30e)
# ---------------------------------------------------------------------------
# Vision prompt: sent with page 1 (and optionally page 2) as image.
# Returns: PII yes/no, document type, field inventory, role attribution.
# One call per file, ~2-3 seconds.

SEGREGATION_PROMPT_VISION = (
    "You are a document classification assistant for a regulatory breach "
    "notification team.  Analyze the document page image and answer:\n\n"
    "1. Does this document contain **personally identifiable information** "
    "(PII) about individuals?  PII includes: names, SSNs, dates of birth, "
    "addresses, phone numbers, email addresses, medical record numbers, "
    "insurance IDs, bank account numbers, driver license numbers, passport "
    "numbers, student IDs, or any data that identifies a specific person.\n\n"
    "2. What **type** of document is this?  Examples: medical_form, "
    "billing_statement, loan_application, tax_form, pay_stub, "
    "insurance_claim, school_record, shipping_document, invoice, "
    "correspondence, legal_filing, report, spreadsheet_export, other.\n\n"
    "3. List every **PII field** visible on the page, with role attribution.  "
    "For each field, indicate whether it belongs to the **primary_subject** "
    "(the person this record is about — patient, student, employee, "
    "account holder) or a **secondary_contact** (parent, guardian, employer, "
    "witness, emergency contact, spouse, provider, institution).\n\n"
    "4. If the document is a **commercial/business** document with only "
    "company names, product serial numbers, or shipping references (no "
    "individual person PII), set pii to false.\n\n"
    "IMPORTANT: Serial numbers, item numbers, barcodes, purchase order "
    "numbers, and tracking numbers are NOT PII.  Company names and business "
    "addresses are NOT PII unless they identify an individual person.\n\n"
    "File name: {file_name}\n"
    "File type: {file_type}\n"
    "Total pages: {total_pages}\n\n"
    "Respond with ONLY this JSON (no markdown, no commentary):\n"
    '{{\n'
    '  "pii": true/false,\n'
    '  "confidence": 0.0-1.0,\n'
    '  "document_type": "string",\n'
    '  "document_subtype": "string or null",\n'
    '  "issuing_entity": "organization name or null",\n'
    '  "country_hint": "ISO 3166-1 alpha-2 country code (e.g. US, GB, IN, DE, BR) '
    'inferred from content such as addresses, currency, tax terms (SSN, NI Number, '
    'PAN, Aadhaar, CPF, etc.), or null if unclear",\n'
    '  "fields": [\n'
    '    {{\n'
    '      "name": "field label as shown on document",\n'
    '      "type": "PERSON|US_SSN|DATE_OF_BIRTH|LOCATION|PHONE_NUMBER|'
    'EMAIL_ADDRESS|MEDICAL_RECORD|INSURANCE_ID|BANK_ACCOUNT|'
    'US_DRIVER_LICENSE|US_PASSPORT|STUDENT_ID|EMPLOYER_ID|OTHER_ID",\n'
    '      "role": "primary_subject|secondary_contact",\n'
    '      "value_visible": true/false\n'
    '    }}\n'
    '  ],\n'
    '  "primary_subject_type": "patient|student|employee|account_holder|'
    'applicant|claimant|taxpayer|other|null",\n'
    '  "summary": "one-sentence description of what this document is"\n'
    '}}'
)


SEGREGATION_PROMPT_TEXT = (
    "You are a document classification assistant for a regulatory breach "
    "notification team.  Analyze the following document text and answer:\n\n"
    "1. Does this document contain **personally identifiable information** "
    "(PII) about individuals?  PII includes: names, SSNs, dates of birth, "
    "addresses, phone numbers, email addresses, medical record numbers, "
    "insurance IDs, bank account numbers, driver license numbers, passport "
    "numbers, student IDs, or any data that identifies a specific person.\n\n"
    "2. What **type** of document is this?\n\n"
    "3. List every **PII field** visible, with role attribution: "
    "**primary_subject** (the person this record is about) or "
    "**secondary_contact** (parent, guardian, employer, witness, etc.).\n\n"
    "4. If the document is a **commercial/business** document with only "
    "company names, product numbers, or shipping references (no individual "
    "person PII), set pii to false.\n\n"
    "IMPORTANT: Serial numbers, item numbers, barcodes, purchase order "
    "numbers, and tracking numbers are NOT PII.\n\n"
    "File name: {file_name}\n"
    "File type: {file_type}\n"
    "Total pages: {total_pages}\n\n"
    "--- DOCUMENT TEXT (first {char_count} characters) ---\n"
    "{document_text}\n"
    "--- END ---\n\n"
    "Respond with ONLY this JSON (no markdown, no commentary):\n"
    '{{\n'
    '  "pii": true/false,\n'
    '  "confidence": 0.0-1.0,\n'
    '  "document_type": "string",\n'
    '  "document_subtype": "string or null",\n'
    '  "issuing_entity": "organization name or null",\n'
    '  "country_hint": "ISO 3166-1 alpha-2 country code (e.g. US, GB, IN, DE, BR) '
    'inferred from content such as addresses, currency, tax terms (SSN, NI Number, '
    'PAN, Aadhaar, CPF, etc.), or null if unclear",\n'
    '  "fields": [\n'
    '    {{\n'
    '      "name": "field label as shown on document",\n'
    '      "type": "PERSON|US_SSN|DATE_OF_BIRTH|LOCATION|PHONE_NUMBER|'
    'EMAIL_ADDRESS|MEDICAL_RECORD|INSURANCE_ID|BANK_ACCOUNT|'
    'US_DRIVER_LICENSE|US_PASSPORT|STUDENT_ID|EMPLOYER_ID|OTHER_ID",\n'
    '      "role": "primary_subject|secondary_contact",\n'
    '      "value_visible": true/false\n'
    '    }}\n'
    '  ],\n'
    '  "primary_subject_type": "patient|student|employee|account_holder|'
    'applicant|claimant|taxpayer|other|null",\n'
    '  "summary": "one-sentence description of what this document is"\n'
    '}}'
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
    "understand_document": UNDERSTAND_DOCUMENT,
    "understand_multi_page_document": UNDERSTAND_MULTI_PAGE_DOCUMENT,
    "understand_document_vision": UNDERSTAND_DOCUMENT_VISION,
    "segregation_vision": SEGREGATION_PROMPT_VISION,
    "segregation_text": SEGREGATION_PROMPT_TEXT,
}
