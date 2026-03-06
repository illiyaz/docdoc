# Implementation Plan — Forentis AI (Active Steps)

Active implementation steps (14-16). For completed steps (Phases 1-4, Steps 1-13), see [PLAN_COMPLETED.md](PLAN_COMPLETED.md).
See [CLAUDE.md](../CLAUDE.md) for project overview and conventions.

**Phase 5 — Forentis AI Evolution (IN PROGRESS)**

Steps 1-13 COMPLETE (1550+ tests). See PLAN_COMPLETED.md for full details.

---

### Step 14 — LLM Document Understanding & Detection Quality (PENDING)

**Goal:** Dramatically reduce false positive rates by introducing an LLM Document Understanding stage that creates a semantic schema of the document BEFORE Presidio runs. The LLM tells Presidio what the document IS, so Presidio's raw pattern matches can be filtered through contextual understanding. Also tighten deterministic fallback patterns for LLM-off mode.

**Observed Problem (real test case — Boosey & Hawkes financial statement):**

A 1-page financial statement for client Adeline Chandler produced **38 PII detections** when the true PII count is ~5. Key failures:
- `001968` (client account number) → matched as COMPANY_NUMBER_UK (70%), US_DRIVER_LICENSE (1%)
- `1121799` (statement reference) → matched as DRIVER_LICENSE_US (65%)
- `"Statement"`, `"Summary"`, `"STREET"` → matched as STUDENT_ID (65%)
- `"Description"`, `"Transactions"` → matched as VAT_EU (75%)
- `30/06/2020` (transaction date) → matched as DATE_OF_BIRTH_DMY (70%)
- `CLIFFORD BARNES` (person in transfer) → matched as ORGANIZATION (85%)
- Entity group for Adeline Chandler included false positive members (COMPANY_NUMBER_UK, US_DRIVER_LICENSE, STUDENT_ID)

Root cause: Presidio runs blind pattern matching with zero document context. Every regex that can match, does match. The LLM entity analysis (Step 13) then works with polluted input and propagates errors.

**Architectural Change: LLM Document Understanding as Semantic Pre-Filter**

```
OLD pipeline:
  Presidio (blind) → 38 detections (85% FP) → LLM groups garbage → bad output

NEW pipeline:
  LLM Document Understanding → DocumentSchema → Presidio + schema filtering → ~5 clean detections → LLM groups clean data → good output

WITHOUT LLM (fallback):
  Improved patterns + context deny-lists → Presidio → ~15-18 detections (~50% FP) → heuristic grouping
```

The LLM does NOT extract PII and does NOT replace Presidio. The LLM creates a **DocumentSchema** — a structured understanding of what this document is, what fields mean, and what's real PII vs. reference numbers. Presidio still does actual extraction, but results are filtered/reclassified through the schema.

#### 14a. DocumentSchema Data Model

**File: `app/structure/document_schema.py`** (new):

```python
from dataclasses import dataclass, field

@dataclass
class FieldContext:
    label: str              # "Tax No.", "Client:", "Statement Nr."
    value_example: str      # "285-07-5085", "001968", "1121799"
    semantic_type: str      # "tax_identification_number", "account_number", "reference_number"
    is_pii: bool            # True for tax_id, False for account_number
    presidio_override: str | None = None  # "US_SSN" — what Presidio SHOULD classify this as
    suppress_types: list[str] = field(default_factory=list)  # ["COMPANY_NUMBER_UK", "US_DRIVER_LICENSE"] — types to suppress for this value

@dataclass
class PersonContext:
    name: str               # "Clifford Barnes"
    role: str               # "related_party", "primary_subject", "institutional"
    context: str            # "Named in transfer transaction"
    is_pii_subject: bool    # True if this person's PII should be extracted

@dataclass
class DateContext:
    value: str              # "30/06/2020"
    semantic_type: str      # "transaction_date", "statement_period_end", "date_of_birth"
    is_pii: bool            # False for transaction_date, True for DOB

@dataclass
class TableColumn:
    header: str             # "Date", "Ref.", "Description", "Amount"
    semantic_type: str      # "transaction_date", "reference_number", "description_text", "currency_amount"
    contains_pii: bool      # False for transactional columns, True for PII columns (e.g., "SSN", "Name")
    pii_type: str | None    # If contains_pii=True, what Presidio type (e.g., "US_SSN"). None otherwise.

@dataclass
class TableSchema:
    columns: list[TableColumn]
    row_count_estimate: int         # helps reviewer understand data volume per table
    table_context: str              # "Transaction history table", "Employee payroll records"
    table_location: str | None      # "page_1_lower_half", "page_3" — rough location for matching
    has_pii_columns: bool           # convenience flag: any column has contains_pii=True

@dataclass
class DocumentSchema:
    document_type: str              # "financial_statement", "medical_record", "hr_file", "insurance_claim"
    document_subtype: str | None    # "royalty_statement", "bank_statement", "invoice"
    issuing_entity: str | None      # "Boosey & Hawkes, Inc."
    
    field_map: list[FieldContext]    # labeled fields and what they actually are
    people: list[PersonContext]      # people identified with roles
    organizations: list[str]        # orgs identified (issuer, employers, etc.)
    date_contexts: list[DateContext] # dates with their actual meaning
    tables: list[TableSchema]       # zero or more tables detected on the page
    
    suppression_hints: list[str]    # free-text hints: "001968 is client account number, not ID"
    extraction_notes: str           # "This is a single-individual financial statement"
    
    # Confidence in the schema itself
    schema_confidence: float        # 0.0-1.0, how confident the LLM is in this understanding
    detected_by: str                # "llm" | "heuristic" | "llm+heuristic"
```

#### 14b. LLM Document Understanding Prompt

**File: `app/llm/prompts.py`** — New template `UNDERSTAND_DOCUMENT`:

The prompt sends the LLM:
1. Full text of the onset page (masked if `pii_masking_enabled=true`)
2. Document metadata (file name, file type, structure_class)
3. Heuristic structure analysis (doc type guess, sections detected)

The LLM produces a DocumentSchema JSON:

```
You are analyzing a document to understand its structure and identify what data fields mean.

Document: {file_name} ({file_type}, {structure_class})
Heuristic analysis suggests: {heuristic_doc_type}

--- DOCUMENT TEXT (page {onset_page}) ---
{page_text}
--- END ---

Analyze this document and respond ONLY with a JSON object:
{
  "document_type": "the type of document (financial_statement, medical_record, hr_file, insurance_claim, legal_filing, tax_form, correspondence, etc.)",
  "document_subtype": "more specific type if identifiable",
  "issuing_entity": "the organization that produced this document, or null",
  "field_map": [
    {
      "label": "the field label as it appears in the document",
      "value_example": "the value next to this label",
      "semantic_type": "what this field actually represents (tax_id, account_number, reference_number, phone_number, address, etc.)",
      "is_pii": true/false,
      "presidio_override": "if is_pii, what Presidio entity type this should be classified as, else null",
      "suppress_types": ["list of Presidio entity types that should NOT match this value"]
    }
  ],
  "people": [
    {
      "name": "person's name",
      "role": "primary_subject | related_party | institutional_contact | provider",
      "context": "how this person relates to the document",
      "is_pii_subject": true/false
    }
  ],
  "organizations": ["list of organizations mentioned"],
  "date_contexts": [
    {
      "value": "the date as it appears",
      "semantic_type": "transaction_date | statement_period | date_of_birth | filing_date | etc.",
      "is_pii": true/false
    }
  ],
  "tables": [
    {
      "columns": [
        {
          "header": "the column header text",
          "semantic_type": "what this column contains (transaction_date, reference_number, person_name, government_id, currency_amount, description_text, etc.)",
          "contains_pii": true/false,
          "pii_type": "if contains_pii, the Presidio entity type (US_SSN, PERSON, EMAIL_ADDRESS, etc.), else null"
        }
      ],
      "row_count_estimate": 0,
      "table_context": "what this table represents (e.g., 'Transaction history', 'Employee payroll records')",
      "has_pii_columns": true/false
    }
  ],
  "suppression_hints": ["free text hints about values that look like PII but aren't"],
  "extraction_notes": "brief note about what PII to expect and how it's organized"
}

IMPORTANT:
- Be precise about what IS and ISN'T PII. Reference numbers, account IDs, and statement numbers are NOT PII.
- Phone/fax numbers belonging to organizations (not individuals) should be marked is_pii=false.
- Dates that are transaction dates, statement periods, or filing dates are NOT dates of birth.
- Short numeric codes (under 8 digits) next to labels like "Client:", "Ref:", "Statement Nr:" are reference numbers, NOT government IDs.
- For tables: identify EVERY table on the page. Mark each column as contains_pii or not. Transaction tables (date, ref, description, amount) typically have ZERO PII columns. Payroll/HR tables (name, SSN, DOB, salary) have MULTIPLE PII columns. Mixed tables (name, department, office) may have partial PII.
- A table column containing amounts, reference numbers, descriptions, or status values is NOT a PII column even if Presidio would match patterns in the data.
```

#### 14c. Document Schema Filter (Post-Presidio)

**File: `app/pii/schema_filter.py`** (new):

```python
class SchemaFilter:
    """Filters Presidio detections through a DocumentSchema to remove false positives."""
    
    def __init__(self, schema: DocumentSchema):
        self.schema = schema
        self._build_suppression_index()
        self._build_table_index()
    
    def filter_detections(self, detections: list[Detection]) -> list[Detection]:
        """
        For each Presidio detection:
        1. Check if the detected value matches a field_map entry
           - If field says is_pii=False → SUPPRESS (log reason)
           - If field says presidio_override → RECLASSIFY to override type
           - If detection type is in field's suppress_types → SUPPRESS
        2. Check table_schemas — if detection falls within a table region:
           - If table has_pii_columns=False → SUPPRESS all detections from table text
           - If table has PII columns, check if detection matches a PII column → KEEP
           - If detection matches a non-PII column (ref, amount, description) → SUPPRESS
        3. Check date_contexts — if date matches and is_pii=False → SUPPRESS
        4. Check people — if ORGANIZATION matches a known person name → RECLASSIFY to PERSON
        5. Check suppression_hints — keyword match → SUPPRESS
        6. If no schema match → KEEP (Presidio detection passes through)
        
        Returns: filtered list + suppression log for audit trail
        """
    
    def _build_table_index(self):
        """
        Builds a lookup from table column headers to their semantic types.
        Used to match Presidio detections against known table structure.
        
        For tables with has_pii_columns=False, ALL values appearing between
        table header keywords are suppressed. This handles the common case
        where PyMuPDF flattens table text into a single stream:
        
        "Date Ref. Description Amount 30/06/2020 001967 Heirs Transfer 65.29 CR"
        
        The filter identifies that 30/06/2020, 001967, and 65.29 are all
        table cell values (not PII) based on their proximity to known
        non-PII column headers.
        """
    
    def get_suppression_log(self) -> list[dict]:
        """Returns audit log of every suppressed/reclassified detection with reason."""
```

**Table-aware filtering — how it works with flattened PDF text:**

When PyMuPDF extracts a table, the column structure is lost. The filter uses two strategies:

**Strategy 1: Header proximity (for flattened text)**
If the schema says columns are `["Date", "Ref.", "Description", "Amount"]` and none contain PII, any Presidio detection occurring in text between/near these header keywords is suppressed.

```
Extracted text: "Date Ref. Description Amount 30/06/2020 001967 Heirs Transfer from CLIFFORD BARNES 65.29 CR"
                 ^^^^  ^^^^  ^^^^^^^^^^^  ^^^^^^  ← headers detected, none are PII columns
                                                   → everything after headers is table data → SUPPRESS all matches
```

**Strategy 2: Column-value mapping (for structured extraction)**
For CSV/Excel or well-extracted PDF tables where column association is preserved, the filter maps each value to its column and checks `contains_pii`:

```
Column "Ref." → semantic_type="reference_number" → contains_pii=False
  Value "001967" matched as DRIVER_LICENSE_US → SUPPRESS (non-PII column)

Column "Name" → semantic_type="person_name" → contains_pii=True, pii_type=PERSON
  Value "Alice Smith" matched as PERSON → KEEP (PII column confirms)
```

**Schema filter behavior for the Boosey & Hawkes example:**

| Presidio Detection | Schema Match | Action | Result |
|---|---|---|---|
| `001968` → COMPANY_NUMBER_UK (70%) | field_map: "Client: 001968" → account_number, is_pii=False | SUPPRESS | Removed |
| `001968` → US_DRIVER_LICENSE (1%) | Same field_map entry, suppress_types includes US_DRIVER_LICENSE | SUPPRESS | Removed |
| `1121799` → DRIVER_LICENSE_US (65%) | field_map: "Statement Nr.: 1121799" → reference_number | SUPPRESS | Removed |
| `001967` → DRIVER_LICENSE_US (65%) | table: "Ref." column → reference_number, contains_pii=False | SUPPRESS | Removed |
| `30/06/2020` → DATE_OF_BIRTH_DMY (70%) | table: "Date" column → transaction_date, contains_pii=False | SUPPRESS | Removed |
| `65.29` → (any numeric match) | table: "Amount" column → currency_amount, contains_pii=False | SUPPRESS | Removed |
| `"Statement"` → STUDENT_ID (65%) | suppression_hints: common English words | SUPPRESS | Removed |
| `"Description"` → VAT_EU (75%) | table: column header text, not data | SUPPRESS | Removed |
| `CLIFFORD BARNES` → ORGANIZATION (85%) | people: "Clifford Barnes", role=related_party | RECLASSIFY | → PERSON |
| `285-07-5085` → US_SSN (90%) | field_map: "Tax No.: 285-07-5085" → tax_id, presidio_override=US_SSN | KEEP (confirmed) | US_SSN ✓ |
| `ADELINE CHANDLER` → PERSON (90%) | people: "Adeline Chandler", role=primary_subject | KEEP (confirmed) | PERSON ✓ |

**Table filtering for different document types:**

| Document Type | Table Content | has_pii_columns | Filter Behavior |
|---|---|---|---|
| Financial statement | Date, Ref, Description, Amount | `False` | Suppress ALL detections from table region |
| Employee payroll | Name, SSN, DOB, Salary, Department | `True` (Name, SSN, DOB) | Keep PII column detections, suppress Department/Salary matches |
| Insurance claim | Claim#, Date Filed, Provider, Diagnosis, Amount | `True` (Provider, Diagnosis=PHI) | Keep Provider/Diagnosis, suppress Claim#/Amount |
| Transaction log | Timestamp, Transaction ID, IP Address, User Agent | `True` (IP Address) | Keep IP, suppress Transaction ID/User Agent |

Expected result: **38 detections → 5-6 clean detections** (~85% false positive reduction).

#### 14d. Deterministic Fallback Improvements (No-LLM Mode)

When `llm_assist_enabled=False`, the DocumentSchema is not available. Improve the deterministic pipeline to reduce false positives independently:

**File: `app/pii/layer1_patterns.py`** — Tighten existing patterns:

| Fix | Pattern | Change |
|---|---|---|
| STUDENT_ID | Currently matches too broadly | Add deny-list: common English words (Statement, Summary, Street, Description, etc.). Require at least 1 digit in the match. |
| COMPANY_NUMBER_UK | Matches short numeric strings | Require exactly 8 characters (UK company numbers are 8 digits). Add context: must appear near "company", "registration", "Ltd", "PLC". |
| DRIVER_LICENSE_US | Matches 5-7 digit numbers | Add state-specific format validation. Require context: near "license", "DL", "driver". |
| VAT_EU | Matches column headers | Require country prefix (GB, DE, FR, etc.) followed by digits. Current pattern is too permissive. |
| DATE_OF_BIRTH_DMY | Matches any date | Add context requirement: must appear near "DOB", "born", "birth", "date of birth". Dates near "transaction", "period", "statement" → suppress. |

**File: `app/pii/context_deny_list.py`** (new):

```python
# Common English words that trigger false positive entity matches
COMMON_WORD_DENY_LIST = frozenset({
    "statement", "summary", "description", "street", "transactions",
    "balance", "payment", "opening", "amount", "total", "period",
    "account", "reference", "number", "date", "page", "report",
    "invoice", "receipt", "credit", "debit", "transfer", "other",
})

# Labels that indicate the adjacent value is a reference number, not PII
REFERENCE_LABELS = frozenset({
    "ref", "ref.", "reference", "ref no", "ref no.", "statement nr",
    "statement no", "invoice no", "account no", "client no", "client",
    "case no", "case id", "file no", "policy no", "claim no",
    "order no", "receipt no", "confirmation no", "tracking no",
})

def is_likely_false_positive(detected_text: str, entity_type: str, 
                              surrounding_text: str) -> bool:
    """
    Heuristic check: is this detection likely a false positive?
    Uses deny lists and context patterns to catch obvious FPs.
    Returns True if detection should be suppressed.
    """
```

**Expected improvement without LLM:** ~85% FP rate → ~40-50% FP rate. Not as good as with LLM (→ ~10-15%), but significantly better than current state.

#### 14e. Updated Pipeline Stages

```
WITH LLM (AI-Enhanced mode):
  discovery → cataloging → verified_onset → 
  ★ document_understanding (LLM → DocumentSchema) → 
  sample_extraction (Presidio + schema filter) → 
  entity_analysis (LLM groups clean data) → 
  auto_approve → [review] → full_extraction → RRA → notification

WITHOUT LLM (Deterministic fallback):
  discovery → cataloging → verified_onset → 
  structure_analysis (heuristic, existing) → 
  sample_extraction (Presidio + improved patterns + deny-lists) → 
  auto_approve → [review] → full_extraction → RRA → notification
```

Note: `structure_analysis` (Step 11 heuristic) is ABSORBED into `document_understanding` when LLM is available. The LLM does everything the heuristic did plus field mapping, semantic typing, and suppression hints — all in one call. When LLM is off, the heuristic runs as before with improved patterns.

#### 14f. Schema Integration with Entity Analysis (Step 13)

When DocumentSchema is available, the entity analysis (Step 13) benefits:
- Entity groups built from schema's `people` list (not from noisy Presidio detections)
- `PersonContext.role` feeds directly into entity role attribution
- `PersonContext.is_pii_subject` determines notification relevance
- Relationships inferred from document type + people roles
- LLM entity analysis prompt can include the schema for additional context

```python
# In app/structure/llm_entity_analyzer.py
def analyze(self, blocks, sample_detections, structure_analysis, 
            document_id, document_schema: DocumentSchema | None = None):
    """
    If document_schema is provided, use it to:
    1. Pre-seed entity groups from schema.people
    2. Filter sample_detections through schema before analysis
    3. Include schema context in the LLM prompt
    """
```

#### 14g. Suppression Audit Trail

Every suppressed or reclassified detection is logged for governance:

**New table or extension to `llm_call_logs`:**

```python
# Option: extend extractions table or create new audit entries
suppression_log_entry = {
    "document_id": "uuid",
    "original_type": "COMPANY_NUMBER_UK",
    "original_value_masked": "001***",
    "original_confidence": 0.70,
    "action": "suppressed",           # suppressed | reclassified | confirmed
    "reason": "field_map: account_number, is_pii=False",
    "schema_field": "Client: 001968",
    "new_type": None,                  # set for reclassified
    "detected_by": "llm_schema_filter" # or "deny_list_filter" for no-LLM mode
}
```

Stored in `AuditEvent` table (existing) with event_type `"detection_suppressed"` or `"detection_reclassified"`.

#### 14h. Implementation Phases

**Phase 14a (do first — deterministic improvements, no LLM required):**
1. Create `app/pii/context_deny_list.py` with COMMON_WORD_DENY_LIST and REFERENCE_LABELS
2. Tighten STUDENT_ID, COMPANY_NUMBER_UK, DRIVER_LICENSE_US, VAT_EU, DATE_OF_BIRTH patterns in `layer1_patterns.py`
3. Add `is_likely_false_positive()` check in detection pipeline
4. Add tests with the Boosey & Hawkes financial statement as a regression test case
5. Measure: should reduce 38 → ~15-18 detections on this document

**Phase 14a-ii (protocol-driven recognizer filtering, no LLM required):**
1. Add `PROTOCOL_DEFAULT_ENTITIES` to `app/core/constants.py` — default entity types per protocol
2. Modify `presidio_engine.py` to accept `target_entity_types` param, pass to Presidio
3. Wire `protocol_config` → `DetectionTask` → `PresidioEngine` so only relevant recognizers run
4. Update frontend protocol form defaults to match backend
5. Measure: GDPR on non-US doc should drop FPs by ~50% (US recognizers disabled), DPDPA similar

**Phase 14b (LLM Document Understanding):**
1. Create `app/structure/document_schema.py` with all dataclasses (including TableColumn, TableSchema)
2. Add `UNDERSTAND_DOCUMENT` prompt template to `app/llm/prompts.py`
3. Create `app/structure/llm_document_understanding.py` — sends onset page to LLM, parses DocumentSchema
4. Create `app/pii/schema_filter.py` — SchemaFilter class with table-aware filtering
5. Update `app/pipeline/two_phase.py` — insert `document_understanding` stage after `verified_onset`
6. Wire schema filter into sample_extraction and full_extraction stages
7. Add tests: schema creation, filtering, table filtering, suppression log, fallback behavior
8. Measure: should reduce 38 → 5-6 detections on this document

**Phase 14c (detection tuning + integration + Catalog UX):**

Post-14b testing revealed three remaining detection quality issues and one UX problem:

**Detection tuning issues (observed):**

1. **Low-confidence FPs still showing:** US_DRIVER_LICENSE at 1% for values 001968, 1121799, 001967 on the Boosey & Hawkes doc. The schema filter should have caught these but the 1% confidence detections slip through. Fix: add a minimum confidence floor (drop detections below 10% confidence before they reach the schema filter).

2. **Dollar amounts matching as phone numbers:** On the Washington CMD employment record, payroll amounts like `153.84 160.00`, `252.73 505.46`, `493.25 986.50`, `0.30 0.60` match PHONE_NUMBER at 40%. These are pairs of decimal numbers (current pay + YTD) that Presidio reads as phone-number-length digit strings. Fix: add a currency pattern detector to `context_deny_list.py` — two decimal numbers (###.##) adjacent to each other are financial data, not phone numbers.

3. **Duplicate detections for same value/page:** Same PERSON "Kristin B Aleshire" appearing twice, same LOCATION "Hagerstown" appearing 4+ times on the same page. Fix: deduplicate detections by (value, entity_type, page) — keep highest confidence match only.

**Expected improvement:** Boosey & Hawkes 8 → ~4, Washington CMD 16 → ~6-7.

**Catalog UX issue (observed):**

After uploading files, the Catalog tab shows "Job Complete — Subjects found: 0, Notification required: 0" which is from a previous unrelated job. The user sees this immediately after upload and thinks the analysis already ran and found nothing. The upload zone, job status, and catalog stats are all mixed together without clear state separation.

Fix: Clear separation between upload state and job results. After upload, show "X files uploaded, ready for analysis" with a clear CTA to the Jobs tab. Previous job results should be in a collapsible section, not prominently displayed next to the upload zone.

**Full Phase 14c scope:**

1. **Detection tuning:**
   - Minimum confidence threshold (10%) — drop sub-threshold detections before processing
   - Currency pattern detector — suppress adjacent decimal number pairs as financial data
   - Detection deduplication — same value + type + page → keep highest confidence only

2. **Integration:**
   - Update entity analysis (Step 13) to accept DocumentSchema
   - Wire suppression audit trail into AuditEvent table
   - Update analysis review API to return DocumentSchema + suppression log
   - Frontend: show document understanding results in review panel (document type, field map, suppression summary)

3. **Catalog UX:**
   - Separate upload state from job results
   - Post-upload: show file count + clear "Run Analysis" CTA
   - Move previous job status to collapsible section
   - Document Catalog stats only show after a job completes for this project

4. **Regression tests:**
   - Boosey & Hawkes: 38 → ~4 clean detections
   - Washington CMD: 68 → ~6-7 clean detections
   - Entity groups correct for both documents

#### Execution Prompts

**Phase 14a prompt (deterministic improvements):**

```
@agent-general-purpose Read CLAUDE.md and docs/PLAN.md Step 14 for context.

Phase 14a: Deterministic false positive reduction (no LLM required).

1. Create app/pii/context_deny_list.py:
   - COMMON_WORD_DENY_LIST: frozenset of English words that trigger FPs
     (statement, summary, description, street, transactions, balance,
     payment, opening, amount, total, period, reference, etc.)
   - REFERENCE_LABELS: frozenset of field labels that indicate adjacent
     value is a reference number (ref, statement nr, invoice no, client,
     account no, case no, policy no, etc.)
   - is_likely_false_positive(detected_text, entity_type, surrounding_text)
     function that checks deny lists + context patterns

2. Tighten patterns in app/pii/layer1_patterns.py:
   - STUDENT_ID: require at least 1 digit, reject COMMON_WORD_DENY_LIST matches
   - COMPANY_NUMBER_UK: require exactly 8 chars, require company context
   - DRIVER_LICENSE_US: add state format validation, require license context
   - VAT_EU: require country prefix + digits, reject common words
   - DATE_OF_BIRTH: require birth/DOB context, suppress near transaction/period/statement

3. Integrate is_likely_false_positive() into the detection pipeline
   (app/tasks/detection.py or app/pii/presidio_engine.py) as a post-filter.
   Suppressed detections should be logged, not silently dropped.

4. Create tests/test_deny_list.py:
   - COMMON_WORD_DENY_LIST entries suppress STUDENT_ID/VAT_EU matches
   - REFERENCE_LABELS suppress DRIVER_LICENSE/COMPANY_NUMBER matches
   - Real PII (actual SSNs, real driver licenses) still passes through
   - Regression test: simulate Boosey & Hawkes detections, verify FP reduction

5. Run pytest on changed files. Fix failures. Update CLAUDE.md.
```

**Phase 14a-ii: Protocol-Driven Recognizer Filtering (no LLM required)**

The single highest-impact multi-geo improvement without LLM. Currently, every job runs ALL Presidio recognizers regardless of jurisdiction. A GDPR job triggers US_DRIVER_LICENSE, US_SSN, NHS_NUMBER. A DPDPA job triggers COMPANY_NUMBER_UK, VAT_EU. Most multi-geo false positives come from irrelevant recognizers running against documents from a different jurisdiction.

**Fix:** Pass `target_entity_types` from `protocol_configs.config_json` into `presidio_engine.analyze()` so only jurisdiction-relevant recognizers run.

**Impact by protocol:**

| Protocol | Recognizers ENABLED | Recognizers DISABLED (would have been FPs) |
|---|---|---|
| HIPAA | PERSON, US_SSN, PHONE_US, EMAIL, MEDICAL_LICENSE, NPI_NUMBER, DATE_OF_BIRTH | COMPANY_NUMBER_UK, VAT_EU, IBAN_CODE, NHS_NUMBER, AADHAAR |
| GDPR | PERSON, EMAIL, PHONE, IBAN_CODE, EU_TAX_ID, GDPR_SPECIAL | US_SSN, US_DRIVER_LICENSE, US_PASSPORT, US_BANK_NUMBER, CREDIT_CARD (if not PCI scope) |
| DPDPA | PERSON, EMAIL, PHONE, AADHAAR, PAN_CARD, PASSPORT | US_SSN, US_DRIVER_LICENSE, COMPANY_NUMBER_UK, VAT_EU, NHS_NUMBER |
| PCI DSS | CREDIT_CARD, PERSON, EMAIL, PHONE | US_SSN, MEDICAL_LICENSE, IBAN_CODE (unless in scope) |
| State Breach (US) | PERSON, US_SSN, US_DRIVER_LICENSE, EMAIL, PHONE, CREDIT_CARD, US_BANK_NUMBER | COMPANY_NUMBER_UK, VAT_EU, NHS_NUMBER, AADHAAR |
| BIPA | PERSON, BIOMETRIC types, US_SSN | Most financial/health recognizers |

**Expected FP reduction per geo (without LLM):**

| Scenario | Before 14a-ii | After 14a-ii |
|---|---|---|
| US state breach on US doc | ~23 detections (post-14a) | ~18 (removes NHS, UK company, VAT matches) |
| GDPR on German bank statement | ~40+ detections | ~15-20 (removes all US recognizer matches) |
| DPDPA on Indian Aadhaar doc | ~35+ detections | ~12-15 (removes UK, US, EU recognizer matches) |

**Implementation:**

**File: `app/pii/presidio_engine.py`** — Modify `analyze()`:

```python
class PresidioEngine:
    def analyze(self, text: str, *, language: str = "en",
                target_entity_types: list[str] | None = None) -> list[RecognizerResult]:
        """
        Run Presidio analysis on text.
        
        If target_entity_types is provided, only run recognizers that detect
        those entity types. This dramatically reduces false positives for
        multi-geo deployments by disabling irrelevant recognizers.
        
        If target_entity_types is None or empty, run all recognizers (backward compatible).
        """
        entities = target_entity_types if target_entity_types else None
        return self.analyzer.analyze(
            text=text, 
            language=language, 
            entities=entities
        )
```

**File: `app/core/constants.py`** (or extend existing) — Default entity types per base protocol:

```python
PROTOCOL_DEFAULT_ENTITIES = {
    "hipaa": [
        "PERSON", "US_SSN", "PHONE_NUMBER", "EMAIL_ADDRESS", "DATE_OF_BIRTH",
        "MEDICAL_LICENSE", "NPI_NUMBER", "US_DRIVER_LICENSE", "US_PASSPORT",
        "IP_ADDRESS", "URL",
    ],
    "gdpr": [
        "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "IBAN_CODE", "IP_ADDRESS",
        "DATE_OF_BIRTH", "LOCATION", "URL", "CRYPTO",
    ],
    "ccpa": [
        "PERSON", "US_SSN", "US_DRIVER_LICENSE", "US_PASSPORT", "EMAIL_ADDRESS",
        "PHONE_NUMBER", "CREDIT_CARD", "US_BANK_NUMBER", "IP_ADDRESS",
        "DATE_OF_BIRTH", "LOCATION", "URL",
    ],
    "dpdpa": [
        "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "DATE_OF_BIRTH",
        "LOCATION", "IP_ADDRESS", "URL",
        # Custom: AADHAAR, PAN_CARD added via custom recognizers
    ],
    "pci_dss": [
        "CREDIT_CARD", "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER",
        "US_BANK_NUMBER", "IBAN_CODE", "SWIFT_CODE",
    ],
    "state_breach": [
        "PERSON", "US_SSN", "US_DRIVER_LICENSE", "US_PASSPORT", "EMAIL_ADDRESS",
        "PHONE_NUMBER", "CREDIT_CARD", "US_BANK_NUMBER", "DATE_OF_BIRTH",
        "MEDICAL_LICENSE", "IP_ADDRESS", "LOCATION",
    ],
    "bipa": [
        "PERSON", "US_SSN", "EMAIL_ADDRESS", "PHONE_NUMBER",
        # Custom: FINGERPRINT, FACE_GEOMETRY, IRIS_SCAN, VOICEPRINT
    ],
    "ferpa": [
        "PERSON", "US_SSN", "EMAIL_ADDRESS", "PHONE_NUMBER", "DATE_OF_BIRTH",
        "LOCATION", "IP_ADDRESS",
    ],
}
```

**File: `app/tasks/detection.py`** — Wire protocol config into detection:

```python
class DetectionTask:
    def run(self, blocks, *, protocol_config=None, **kwargs):
        # Extract target entities from protocol config
        target_entities = None
        if protocol_config:
            config_json = protocol_config.get("config_json", {})
            target_entities = config_json.get("target_entity_types")
            
            # If no explicit target_entity_types in config, use protocol defaults
            if not target_entities:
                base_protocol = config_json.get("base_protocol_id")
                if base_protocol:
                    target_entities = PROTOCOL_DEFAULT_ENTITIES.get(base_protocol)
        
        # Pass to Presidio — None means run all (backward compatible)
        results = self.engine.analyze(text, target_entity_types=target_entities)
```

**Key design decisions:**
- `target_entity_types` in `config_json` takes precedence (user explicitly configured)
- If not set, falls back to `PROTOCOL_DEFAULT_ENTITIES` based on `base_protocol_id`
- If neither is set, runs all recognizers (full backward compatibility)
- This is a pure filtering mechanism — no new recognizers added, no patterns changed
- Works independently of deny-lists (14a) and DocumentSchema (14b) — all three stack

**File: `frontend/src/pages/ProjectDetail.tsx`** — Update guided protocol form:

The Entity Types checkboxes (Step 9) should auto-populate from `PROTOCOL_DEFAULT_ENTITIES` when a base protocol is selected. Currently, the checkboxes pre-check based on `PROTOCOL_DEFAULTS` in the frontend constants. Ensure these match the backend `PROTOCOL_DEFAULT_ENTITIES`.

**Tests:**

```
tests/test_recognizer_filtering.py:
- HIPAA protocol → US_SSN detected, COMPANY_NUMBER_UK NOT detected
- GDPR protocol → IBAN_CODE detected, US_SSN NOT detected
- DPDPA protocol → PERSON detected, COMPANY_NUMBER_UK NOT detected
- No protocol → all recognizers run (backward compatible)
- Empty target_entity_types → all recognizers run
- Custom entity list → only specified types detected
- Protocol defaults resolve correctly for all 8 base protocols
- Frontend PROTOCOL_DEFAULTS match backend PROTOCOL_DEFAULT_ENTITIES
```

**Phase 14a-ii prompt (protocol-driven recognizer filtering):**

```
@agent-general-purpose Read CLAUDE.md and docs/PLAN.md Step 14 for context.

Phase 14a-ii: Protocol-driven recognizer filtering.

1. Add PROTOCOL_DEFAULT_ENTITIES dict to app/core/constants.py with
   default entity type lists for all 8 base protocols (hipaa, gdpr,
   ccpa, dpdpa, pci_dss, state_breach, bipa, ferpa). See PLAN.md
   Step 14 Phase 14a-ii for exact mapping.

2. Modify app/pii/presidio_engine.py analyze() method to accept an
   optional target_entity_types parameter. When provided, pass it as
   the entities parameter to Presidio's analyzer.analyze(). When None
   or empty, run all recognizers (backward compatible).

3. Update app/tasks/detection.py DetectionTask.run() to accept
   protocol_config. Extract target_entity_types from config_json.
   Fall back to PROTOCOL_DEFAULT_ENTITIES[base_protocol_id] if not
   explicitly set. Pass to presidio_engine.analyze().

4. Update frontend protocol form defaults to match backend
   PROTOCOL_DEFAULT_ENTITIES (ensure the entity type checkboxes
   pre-check the correct types per protocol).

5. Create tests/test_recognizer_filtering.py:
   - HIPAA enables US types, disables UK/EU types
   - GDPR enables EU types, disables US types
   - DPDPA enables India types, disables UK/US/EU types
   - No protocol runs all recognizers
   - target_entity_types in config_json overrides protocol defaults
   - All 8 protocols have valid default entity lists

6. Run pytest on changed files. Fix failures. Update CLAUDE.md.
```

**Phase 14b prompt (LLM Document Understanding):**

```
@agent-general-purpose Read CLAUDE.md and docs/PLAN.md Step 14 for context.

Phase 14b: LLM Document Understanding + Schema Filter.

1. Create app/structure/document_schema.py with dataclasses:
   DocumentSchema, FieldContext, PersonContext, DateContext,
   TableColumn, TableSchema
   (exact definitions in PLAN.md Step 14a)

2. Add UNDERSTAND_DOCUMENT prompt template to app/llm/prompts.py
   (exact prompt in PLAN.md Step 14b, includes tables section).
   Update PROMPT_TEMPLATES dict.

3. Create app/structure/llm_document_understanding.py:
   - LLMDocumentUnderstanding class
   - understand(blocks, heuristic_analysis, document_meta) → DocumentSchema
   - Sends onset page text to Ollama with UNDERSTAND_DOCUMENT prompt
   - Parses JSON response into DocumentSchema (including tables)
   - Falls back gracefully: if LLM fails, returns None

4. Create app/pii/schema_filter.py:
   - SchemaFilter(schema: DocumentSchema) class
   - filter_detections(detections) → filtered detections + suppression log
   - Implements: field_map matching, TABLE-AWARE filtering (non-PII columns
     suppress detections, PII columns confirm detections), date_context
     filtering, people reclassification, suppression_hints matching
   - _build_table_index() — maps table column headers to semantic types,
     handles both structured (column-associated) and flattened (header
     proximity) text extraction
   - get_suppression_log() → audit trail

5. Update app/pipeline/two_phase.py:
   - Insert document_understanding stage after verified_onset
   - Pass DocumentSchema to sample_extraction stage
   - Wire SchemaFilter into extraction post-processing
   - SSE event: {"stage": "document_understanding", "status": "complete"}

6. Create tests/test_document_understanding.py:
   - DocumentSchema creation and serialization (including TableSchema)
   - SchemaFilter: suppression, reclassification, passthrough, audit log
   - Table filtering: non-PII table suppresses all matches, mixed table
     keeps PII columns only, payroll table keeps Name/SSN/DOB
   - Flattened text: header proximity suppression works on single-stream text
   - LLM fallback: None schema → no filtering (Presidio output unchanged)
   - Integration: schema + Presidio → filtered output

7. Run pytest on changed files. Fix failures. Update CLAUDE.md.
```

**Phase 14c prompt (detection tuning + integration + Catalog UX):**

```
@agent-general-purpose Read CLAUDE.md and docs/PLAN.md Step 14 for context.

Phase 14c: Detection tuning, integration, and Catalog UX fixes. Three areas.

=== AREA 1: DETECTION QUALITY TUNING ===

1a. Add minimum confidence threshold to the detection pipeline.
    In app/pii/schema_filter.py (or app/tasks/detection.py if schema_filter
    is not the right place), add a pre-filter that drops ALL detections
    with confidence < 0.10 (10%). This eliminates 1% confidence driver's
    license matches on reference numbers. The threshold should be
    configurable via a constant MIN_DETECTION_CONFIDENCE = 0.10.
    Log dropped detections at debug level.

1b. Add currency/financial pattern detection to app/pii/context_deny_list.py:
    - Add a function is_currency_pattern(text: str) -> bool that detects:
      * Two decimal numbers adjacent to each other: "153.84 160.00"
      * Numbers with comma thousands separators: "1,153.84"
      * Numbers preceded by $ or currency symbols
      * Numbers in patterns like "###.## ###.##" (current + YTD pairs)
    - In is_likely_false_positive(), if entity_type is PHONE_NUMBER and
      the detected text matches a currency pattern, return True.
    - This fixes: "153.84 160.00" → PHONE_NUMBER, "493.25 986.50" → PHONE_NUMBER,
      "0.30 0.60" → PHONE_NUMBER on payroll documents.

1c. Add detection deduplication:
    In app/tasks/detection.py or app/pii/schema_filter.py, add a
    deduplicate_detections(detections) function that:
    - Groups detections by (normalized_value, entity_type, page_number)
    - For each group, keeps only the detection with highest confidence
    - Returns deduplicated list + count of duplicates removed
    - This fixes: PERSON "Kristin B Aleshire" appearing 2x,
      LOCATION "Hagerstown" appearing 4x on same page.

1d. Wire all three into the pipeline in this order:
    Presidio runs → min confidence filter → schema filter → currency filter → dedup
    Each filter logs what it removed for the suppression audit trail.

=== AREA 2: INTEGRATION (14c core) ===

2a. Update entity analysis (Step 13) to accept DocumentSchema.
    In app/structure/llm_entity_analyzer.py, modify analyze() to accept
    an optional document_schema parameter. When provided:
    - Pre-seed entity groups from schema.people
    - Filter sample_detections through schema before analysis
    - Include schema context in the LLM prompt for better grouping

2b. Wire suppression audit trail into AuditEvent table.
    In app/audit/events.py, add two new event types:
    "detection_suppressed" and "detection_reclassified".
    In the schema filter and deny-list filter, log each suppression
    as an AuditEvent with: document_id, original_type, original_value
    (masked), confidence, suppression_reason, filter_source
    (schema_filter | deny_list | min_confidence | dedup | currency).

2c. Update analysis review API (app/api/routes/analysis_review.py):
    GET /jobs/{id}/analysis response should now include:
    - document_schema: the DocumentSchema JSON (if available)
    - suppression_summary: {total_suppressed, by_reason: {schema: N, deny_list: N, ...}}
    - suppressed_detections: list of what was filtered out (for transparency)

2d. Frontend: show document understanding results in the analysis review panel
    (frontend/src/pages/ProjectDetail.tsx AnalysisReviewPanel):
    - Above entity groups, show a "Document Understanding" card:
      * Document type badge (e.g., "financial_statement")
      * Issuing entity if available
      * Field map as a compact table (label → semantic type → PII?)
      * Tables detected with column types
    - Below sample PII, show "Filtered Detections" collapsible:
      * Count: "23 false positives suppressed"
      * Expandable list showing what was filtered and why

=== AREA 3: CATALOG TAB UX FIX ===

3a. Fix the Catalog tab in frontend/src/pages/ProjectDetail.tsx:
    The current layout mixes upload state with previous job results,
    causing confusion. Restructure:

    STATE 1 — No files uploaded yet:
      Show upload zone prominently. No job status visible.
      Document Catalog section says "No documents yet."

    STATE 2 — Files uploaded, no job run:
      Upload zone shows "X files uploaded, ready for analysis"
      Show a prominent button: "Go to Jobs tab to run analysis" or
      an inline "Run Analysis Now" button with protocol selector.
      Document Catalog section says "Upload complete. Run analysis
      to catalog documents."
      Do NOT show "Job Complete: 0 subjects" from a previous job.

    STATE 3 — Job running:
      Upload zone collapses to single line "X files uploaded"
      Show "Analysis in progress..." with link to Jobs tab for details.

    STATE 4 — Job complete:
      Upload zone collapses. "Clear & upload new" link available.
      Document Catalog shows full stats (total, auto-processable,
      manual review, by file type, by structure class).
      "Run New Job" section shows previous job result summary.

3b. The "Run New Job" section in Catalog tab should NOT show old job
    results by default. If there's a completed job from a previous run,
    show it in a collapsed "Previous Results" section, not prominently.

3c. After a successful upload, auto-scroll or highlight the next step
    ("Run Analysis" button) so the user knows what to do.

=== TESTING ===

4. Create tests/test_detection_tuning.py:
   - Min confidence filter: 1% detections dropped, 50%+ detections kept
   - Currency pattern: "153.84 160.00" detected, "1,153.84" detected,
     real phone "212-541-6600" NOT suppressed
   - Dedup: 4x same LOCATION on same page → 1 detection kept
   - End-to-end: Boosey & Hawkes mock → ~4 detections after all filters
   - End-to-end: Washington CMD mock → ~6-7 detections after all filters

5. Add suppression audit trail tests:
   - AuditEvent created for each suppressed detection
   - Event includes masked value, original type, reason, filter source
   - Analysis review API returns suppression_summary

6. Run pytest on ALL changed files. Fix failures up to 3 attempts.
   Document any blockers in docs/BLOCKERS.md. Update CLAUDE.md with
   everything done including test counts.
```

#### Key Constraints

- DocumentSchema is a READ-ONLY overlay — it never modifies Presidio's engine or patterns
- Schema filter is a POST-PROCESSING step — Presidio runs unmodified, then results are filtered
- Without LLM, deterministic improvements (deny-lists, tighter patterns) provide partial benefit
- Suppression is auditable — every filtered detection is logged with reason
- Schema confidence < 0.50 → skip filtering, use raw Presidio output (safety valve)
- Air-gap safe: local Ollama only, no external calls
- Memory-safe: schema is lightweight (~1KB per document), no large objects retained
- The LLM makes ONE call per document (not per detection) — efficient even for large datasets
- PII masking still applies: LLM sees masked values when `pii_masking_enabled=true`

---

### Step 15 — Field-Level Review + Protocol Mapping (PENDING)

**Goal:** Give reviewers granular control over which detections proceed to full extraction, and show how detected PII maps to protocol-required fields. Currently, review is binary (approve/reject entire document). Reviewers need to toggle individual detections on/off and see which protocol requirements are satisfied vs missing.

**Current gap:**

```
Current flow:
  6 detections shown → Approve → ALL 6 extracted from all pages
  No way to say "skip the PHONE_NUMBER false positives"
  No way to see "SSN is required by protocol but not detected"

New flow:
  6 detections shown → toggle each on/off → see protocol mapping → Approve → only selected types extracted
```

#### 15a. Detection Selection Model

**Two-tier toggle system:** type-level bulk control + individual detection override.

**File: `app/db/models.py`** — New table or extend `document_analysis_reviews`:

```python
# Option: new table for per-detection decisions
class DetectionReviewDecision(Base):
    __tablename__ = "detection_review_decisions"
    
    id = Column(UUID, primary_key=True, default=uuid4)
    document_analysis_review_id = Column(UUID, ForeignKey("document_analysis_reviews.id"))
    
    # What was detected
    entity_type = Column(VARCHAR(64), nullable=False)   # "PERSON", "PHONE_NUMBER", etc.
    detected_value_masked = Column(VARCHAR(256))          # "Kristin B A***"
    confidence = Column(Float)
    page = Column(Integer)
    
    # Reviewer decision
    include_in_extraction = Column(Boolean, default=True)  # toggle on/off
    decision_reason = Column(VARCHAR(256), nullable=True)  # "false positive", "duplicate", etc.
    decided_by = Column(VARCHAR(128), nullable=True)       # reviewer ID
    decided_at = Column(DateTime, nullable=True)
    
    # Bulk vs individual
    decision_source = Column(VARCHAR(16), default="default")  # "default" | "bulk_type" | "individual"
```

**Behavior:**

| Action | Effect |
|---|---|
| Page loads | All detections default to `include=True` |
| Toggle type OFF (e.g., PHONE_NUMBER) | All PHONE_NUMBER detections set to `include=False`, `decision_source="bulk_type"` |
| Toggle type back ON | All PHONE_NUMBER detections set to `include=True` |
| Toggle individual detection OFF | That specific detection set to `include=False`, `decision_source="individual"`, overrides bulk |
| Toggle individual back ON | Same, `decision_source="individual"` |
| Approve document | Only detections with `include=True` proceed to Phase 2 full extraction |

#### 15b. Protocol Field Mapping

**File: `app/core/constants.py`** — Add protocol field requirements:

```python
PROTOCOL_REQUIRED_FIELDS = {
    "hipaa": {
        "required": [
            {"field": "Individual Name", "entity_types": ["PERSON"], "criticality": "required"},
            {"field": "Address", "entity_types": ["LOCATION"], "criticality": "required"},
            {"field": "Date of Birth", "entity_types": ["DATE_OF_BIRTH"], "criticality": "if_available"},
            {"field": "SSN", "entity_types": ["US_SSN"], "criticality": "if_available"},
            {"field": "Medical Record", "entity_types": ["MEDICAL_LICENSE", "NPI_NUMBER"], "criticality": "if_available"},
            {"field": "Email", "entity_types": ["EMAIL_ADDRESS"], "criticality": "if_available"},
            {"field": "Phone", "entity_types": ["PHONE_NUMBER", "PHONE_US"], "criticality": "if_available"},
        ],
    },
    "state_breach": {
        "required": [
            {"field": "Individual Name", "entity_types": ["PERSON"], "criticality": "required"},
            {"field": "Address", "entity_types": ["LOCATION"], "criticality": "required"},
            {"field": "SSN", "entity_types": ["US_SSN"], "criticality": "required"},
            {"field": "Driver's License", "entity_types": ["US_DRIVER_LICENSE"], "criticality": "if_available"},
            {"field": "Financial Account", "entity_types": ["CREDIT_CARD", "US_BANK_NUMBER"], "criticality": "if_available"},
            {"field": "Email", "entity_types": ["EMAIL_ADDRESS"], "criticality": "required"},
            {"field": "Phone", "entity_types": ["PHONE_NUMBER", "PHONE_US"], "criticality": "if_available"},
        ],
    },
    "gdpr": {
        "required": [
            {"field": "Data Subject Name", "entity_types": ["PERSON"], "criticality": "required"},
            {"field": "Email", "entity_types": ["EMAIL_ADDRESS"], "criticality": "required"},
            {"field": "Address", "entity_types": ["LOCATION"], "criticality": "if_available"},
            {"field": "Phone", "entity_types": ["PHONE_NUMBER"], "criticality": "if_available"},
            {"field": "National ID", "entity_types": ["IBAN_CODE", "EU_TAX_ID"], "criticality": "if_available"},
            {"field": "IP Address", "entity_types": ["IP_ADDRESS"], "criticality": "if_available"},
        ],
    },
    "dpdpa": {
        "required": [
            {"field": "Data Principal Name", "entity_types": ["PERSON"], "criticality": "required"},
            {"field": "Email", "entity_types": ["EMAIL_ADDRESS"], "criticality": "required"},
            {"field": "Phone", "entity_types": ["PHONE_NUMBER"], "criticality": "required"},
            {"field": "Aadhaar", "entity_types": ["AADHAAR"], "criticality": "if_available"},
            {"field": "PAN", "entity_types": ["PAN_CARD"], "criticality": "if_available"},
            {"field": "Address", "entity_types": ["LOCATION"], "criticality": "if_available"},
        ],
    },
    "pci_dss": {
        "required": [
            {"field": "Cardholder Name", "entity_types": ["PERSON"], "criticality": "required"},
            {"field": "Card Number", "entity_types": ["CREDIT_CARD"], "criticality": "required"},
            {"field": "Email", "entity_types": ["EMAIL_ADDRESS"], "criticality": "if_available"},
            {"field": "Phone", "entity_types": ["PHONE_NUMBER"], "criticality": "if_available"},
        ],
    },
    # bipa, ferpa, ccpa, hitech follow same pattern
}
```

**File: `app/api/routes/analysis_review.py`** — New endpoint:

```
GET /jobs/{id}/documents/{doc_id}/protocol-mapping
```

Response:

```json
{
  "protocol": "state_breach",
  "field_mapping": [
    {
      "field": "Individual Name",
      "criticality": "required",
      "status": "detected",
      "matched_detections": [
        {"entity_type": "PERSON", "value_masked": "Kristin B A***", "confidence": 0.85, "included": true}
      ]
    },
    {
      "field": "SSN",
      "criticality": "required",
      "status": "missing",
      "matched_detections": []
    },
    {
      "field": "Phone",
      "criticality": "if_available",
      "status": "needs_review",
      "matched_detections": [
        {"entity_type": "PHONE_NUMBER", "value_masked": "153.84***", "confidence": 0.40, "included": true}
      ]
    }
  ],
  "coverage": {
    "required_fields": 4,
    "required_detected": 2,
    "required_missing": 2,
    "completeness_pct": 50
  }
}
```

#### 15c. Frontend: Detection Review Panel

**File: `frontend/src/pages/ProjectDetail.tsx`** — Redesign AnalysisReviewPanel:

**Layout (top to bottom):**

```
┌─────────────────────────────────────────────────────────────────┐
│ Document Summary                                                │
│ "Employment record for Kristin B Aleshire..."                   │
│ Entity Groups: [Kristin B Aleshire (Employee) 95%]              │
├─────────────────────────────────────────────────────────────────┤
│ Protocol Mapping: State Breach (US)           Completeness: 50% │
│                                                                 │
│ ✅ Individual Name    PERSON Kristin B A*** (85%)     [included] │
│ ✅ Address            LOCATION Hagerstown, MD (85%)   [included] │
│ ⚠️  SSN               Not detected                    MISSING   │
│ ⚠️  Driver's License   Not detected                    MISSING   │
│ ❓ Phone              PHONE_NUMBER 153.84... (40%)    [review]  │
│ ─  Email              Not detected                    optional  │
├─────────────────────────────────────────────────────────────────┤
│ Detection Controls                                              │
│                                                                 │
│ By Type:                                                        │
│ [✓] PERSON (2 detections)           [toggle all on/off]         │
│ [✓] LOCATION (3 detections)         [toggle all on/off]         │
│ [ ] PHONE_NUMBER (2 detections)     [toggle all on/off] ← OFF  │
│                                                                 │
│ Individual Detections:                                          │
│ [✓] PERSON Kristin B Aleshire 85%     p.1                       │
│ [ ] PERSON KRISTIN B ALESHIRE 85%     p.1   ← toggled off (dup)│
│ [✓] LOCATION Hagerstown 85%           p.1                       │
│ [✓] LOCATION MD 85%                   p.1                       │
│ [ ] PHONE_NUMBER 153.84 160.00 40%    p.1   ← toggled off (FP) │
│ [ ] PHONE_NUMBER 056.32 505.46 40%    p.1   ← toggled off (FP) │
│ [✓] LOCATION WASCO 85%                p.1                       │
├─────────────────────────────────────────────────────────────────┤
│ [Approve with selections]  [Reject]                             │
└─────────────────────────────────────────────────────────────────┘
```

**Interaction details:**

- Type-level toggle: checkbox next to type name, toggles all detections of that type
- Individual toggle: checkbox next to each detection chip
- Individual override: if user toggles PHONE_NUMBER type OFF, then turns one specific phone back ON, that individual override is preserved
- Color coding: included = green chip, excluded = grey chip with strikethrough
- Protocol mapping section: green checkmark = detected & included, orange warning = required but missing, grey dash = optional and missing, blue question = detected but needs review (low confidence)
- Completeness percentage: `required_detected / required_fields * 100`
- "Approve with selections" button: sends the inclusion decisions to backend, only included types proceed to Phase 2

**File: `app/api/routes/analysis_review.py`** — Update approve endpoint:

```
POST /jobs/{id}/documents/{doc_id}/approve
Body: {
  "rationale": "Approved with field selections",
  "detection_decisions": [
    {"entity_type": "PERSON", "detected_value_masked": "Kristin B A***", "page": 1, "include": true},
    {"entity_type": "PHONE_NUMBER", "detected_value_masked": "153.84***", "page": 1, "include": false, "reason": "false positive - dollar amount"}
  ]
}
```

Phase 2 extraction then only runs recognizers for **included entity types** on approved documents.

#### 15d. Schema + Migration

**Migration 0009:** `detection_review_decisions` table (9 columns). Extends `document_analysis_reviews` with `selected_entity_types` JSON column (stores the final approved type list).

#### 15e. Execution Prompt

```
@agent-general-purpose Read CLAUDE.md and docs/PLAN.md Step 15 for context.

Step 15: Field-level detection review + protocol mapping.

=== BACKEND ===

1. Add PROTOCOL_REQUIRED_FIELDS dict to app/core/constants.py with
   field requirements for all 8 base protocols (hipaa, state_breach,
   gdpr, dpdpa, pci_dss, ccpa, bipa, ferpa). Each field has: name,
   entity_types list, criticality (required | if_available).
   See PLAN.md Step 15b for exact structure.

2. Create migration 0009: new table detection_review_decisions with
   columns: id, document_analysis_review_id (FK), entity_type,
   detected_value_masked, confidence, page, include_in_extraction
   (bool default true), decision_reason, decided_by, decided_at,
   decision_source (default/bulk_type/individual).
   Add selected_entity_types JSON column to document_analysis_reviews.

3. Add model DetectionReviewDecision to app/db/models.py.

4. Add GET /jobs/{id}/documents/{doc_id}/protocol-mapping endpoint
   in app/api/routes/analysis_review.py. Returns: protocol field
   requirements, matched detections per field, coverage stats
   (required detected vs missing, completeness percentage).

5. Update POST /jobs/{id}/documents/{doc_id}/approve to accept
   detection_decisions array in request body. Store decisions in
   detection_review_decisions table. Store selected_entity_types
   (the included types) on document_analysis_reviews.

6. Update Phase 2 extraction (app/pipeline/two_phase.py extract_generator):
   read selected_entity_types from document_analysis_reviews. Pass
   as target_entity_types to Presidio so only approved types are
   extracted during full document processing.

=== FRONTEND ===

7. Redesign AnalysisReviewPanel in frontend/src/pages/ProjectDetail.tsx:

   a) Protocol Mapping section (top):
      - Show protocol name and completeness percentage
      - List each required field with status badge:
        green check = detected & included
        orange warning = required but missing
        blue question = detected, low confidence, needs review
        grey dash = optional and not detected
      - Each field row shows matched detections if any

   b) Detection Controls section (middle):
      - Type-level toggles: checkbox per entity type with detection count
        Toggling type off sets all detections of that type to excluded
      - Individual detection list: checkbox per detection chip
        Shows: entity_type, masked value, confidence, page
        Excluded detections shown as grey with strikethrough
      - Individual overrides preserved when type-level toggle changes

   c) Updated approve button:
      - Label: "Approve with selections" (not just "Approve")
      - Sends detection_decisions array to updated approve endpoint
      - Disabled if zero detections included
      - Shows warning if required protocol fields are missing

8. Add API functions to frontend/src/api/client.ts:
   - getProtocolMapping(jobId, docId) → protocol mapping response
   - Updated approveDocument to include detection_decisions

=== TESTS ===

9. Create tests/test_detection_review.py:
   - Default: all detections included
   - Bulk type toggle: all PHONE_NUMBER excluded
   - Individual override: one PHONE re-included after bulk off
   - Approve persists decisions to detection_review_decisions table
   - Phase 2 extraction uses only included types
   - Protocol mapping: required field detected → status "detected"
   - Protocol mapping: required field missing → status "missing"
   - Protocol mapping: completeness percentage correct
   - Migration 0009 creates table with correct columns

10. Run pytest on all changed files. Fix failures up to 3 attempts.
    Update CLAUDE.md.
```

---

### Step 16 — UX Consolidation: Dashboard, Jobs, Sidebar, Density (PENDING)

**Goal:** Comprehensive UX pass to make Forentis AI feel like a polished product, not a developer prototype. Covers four areas: dashboard redesign, Jobs tab improvements, sidebar navigation consolidation, and Density tab clarity.

**Observed problems:**
- Dashboard is a dead end with no actionable content
- Jobs tab: 16+ test jobs with no filtering, no kill button, no filename, stuck "running" job
- Sidebar: 8 nav items, 4 are separate review queues, "Submit Job" is redundant, "Diagnostic" is developer-facing
- Density tab: purpose unclear, empty before extraction, no context for when data appears

---

#### 16a. Dashboard Redesign

**File: `frontend/src/pages/Dashboard.tsx`** — Complete rewrite.

**Layout:**

```
┌─────────────────────────────────────────────────────────────────┐
│ Forentis AI                                       [+ New Project]│
├─────────────────────────────────────────────────────────────────┤
│ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────┐│
│ │ 3 Active     │ │ 2 Pending    │ │ 5 Jobs This  │ │ 1,247    ││
│ │ Projects     │ │ Reviews      │ │ Week         │ │ Documents││
│ └──────────────┘ └──────────────┘ └──────────────┘ └──────────┘│
│                                                                 │
│ ⚠️  Needs Attention                                             │
│ │ 📋 Elmer breach — 1 doc pending review              [Review] ││
│ │ 📋 Client XYZ — 3 docs pending review               [Review] ││
│                                                                 │
│ 🏃 Running Jobs                                                 │
│ │ ⏳ Client XYZ — Extracting (45%) — 12 docs           [View]  ││
│                                                                 │
│ 📊 Active Projects                                              │
│ │ Elmer breach    12 docs   Last: 2 min ago    1 pending       ││
│ │ Client XYZ      45 docs   Last: 1 hr ago     3 pending      ││
│                                                                 │
│ 📜 Recent Activity (last 20)                                    │
│ │ 2 min ago   Job completed — Elmer breach (1 doc)             ││
│ │ 15 min ago  Document approved — Client XYZ (payroll.pdf)     ││
│ │ 1 hr ago    Export generated — Client XYZ (CSV, 45 subjects) ││
└─────────────────────────────────────────────────────────────────┘
```

**Backend: `app/api/routes/dashboard.py`** (new):

```
GET /dashboard/summary
```

Returns:

```json
{
  "stats": {
    "active_projects": 3,
    "pending_reviews": 2,
    "jobs_this_week": 5,
    "documents_processed": 1247
  },
  "needs_attention": [
    {"project_id": "uuid", "project_name": "Elmer breach", "pending_count": 1, "oldest_pending_at": "..."}
  ],
  "running_jobs": [
    {"job_id": "uuid", "project_name": "Client XYZ", "status": "extracting", "progress_pct": 45, "document_count": 12}
  ],
  "active_projects": [
    {"id": "uuid", "name": "Elmer breach", "document_count": 12, "last_activity_at": "...", "pending_reviews": 1}
  ],
  "recent_activity": [
    {"type": "job_completed", "project_name": "Elmer breach", "detail": "1 document analyzed", "timestamp": "..."}
  ]
}
```

All data from existing tables — no new tables. Queries: projects count, document_analysis_reviews pending count, ingestion_runs this week, documents total, running ingestion_runs, recent events union (jobs + approvals + exports).

**Frontend behavior:**
- Stat cards clickable (projects → /projects, reviews → first pending project)
- Running jobs auto-refresh every 10s, dashboard overall every 30s
- Empty states: "Create your first project" when no projects, "All clear" when no pending reviews
- Running jobs section hidden when none exist

---

#### 16b. Jobs Tab Improvements

**File: `frontend/src/pages/ProjectDetail.tsx`** — JobsTab component overhaul.

**Changes:**

| Fix | Details |
|---|---|
| **Add filename column** | Show first document filename (truncated to 30 chars, tooltip for full). Query from documents table via ingestion_run_id. |
| **Cancel/kill button** | Red "Cancel" button on running/pending jobs. Calls `POST /jobs/{id}/cancel`. Sets status to `cancelled`. Running tasks get interrupted. |
| **Status filter** | Dropdown above table: All, Running, Analyzed, Completed, Failed, Cancelled. Filters job list client-side. |
| **Pagination** | Show 10 jobs per page. Page controls at bottom. Or virtual scroll for large lists. |
| **Fix duration for analyzed jobs** | Calculate duration from created_at to analysis_completed_at (not just completed_at). Show "2m 15s (analyze)" for analyzed, "13m 50s (full)" for completed. |
| **Delete old jobs** | Trash icon on completed/failed/cancelled jobs. Soft delete (set status='archived', hide from default view). "Show archived" toggle to reveal. |
| **Run New Job button** | Add prominent "▶ Run New Job" button in Jobs tab header (same as Catalog tab's run functionality). Opens protocol selector inline. |
| **Sort controls** | Click column headers to sort by date, status, duration. Default: newest first. |

**Backend additions:**

```
POST /jobs/{id}/cancel    → Sets status='cancelled', returns updated job
DELETE /jobs/{id}          → Soft delete: status='archived'
GET /projects/{id}/jobs?status=analyzed&page=1&per_page=10  → Filtering + pagination
```

**File: `app/api/routes/jobs.py`** — Add cancel and archive endpoints. Update project jobs endpoint with query params.

---

#### 16c. Sidebar Navigation Consolidation

**File: `frontend/src/App.tsx`** — Restructure sidebar navigation.

**Current (8 items, cluttered):**
```
Dashboard
Projects
Low Confidence     ← separate review queue
Escalation         ← separate review queue
QC Sampling        ← separate review queue
RRA Review         ← separate review queue
Submit Job         ← redundant
Diagnostic         ← developer tool
```

**New (5 items, clean):**
```
Dashboard          ← redesigned (16a)
Projects           ← unchanged
Review Queue       ← NEW: merged page with filter tabs
Jobs               ← renamed Submit Job, now shows all jobs across projects
Settings           ← NEW: app config, LLM status
```

**Review Queue merge — `frontend/src/pages/ReviewQueue.tsx`** (new):

Single page with tab bar:

```
┌─────────────────────────────────────────────────────────────────┐
│ Review Queue                                     Total: 12 items│
│                                                                 │
│ [All (12)] [Low Confidence (4)] [Escalation (2)] [QC (3)] [RRA (3)]│
│                                                                 │
│ ┌───────────────────────────────────────────────────────────────┐│
│ │ Project          Document              Type      Status      ││
│ │ Elmer breach     WashingtonCMD.pdf     QC        pending     ││
│ │ Client XYZ       payroll.pdf           Low Conf  pending     ││
│ │ Client XYZ       insurance.pdf         Escalation pending    ││
│ │ ...                                                          ││
│ └───────────────────────────────────────────────────────────────┘│
│                                                                 │
│ Click any row to open review detail                             │
└─────────────────────────────────────────────────────────────────┘
```

- Tabs filter by queue type (low_confidence, escalation, qc_sampling, rra_review)
- "All" tab shows everything sorted by oldest first
- Each row shows project name, document name, queue type, status, age
- Clicking opens the existing review detail UI (reuse current components)
- Badge on sidebar shows total pending count

**Jobs page (sidebar) — `frontend/src/pages/Jobs.tsx`** (rename/rework Submit Job):

Global jobs view across all projects. Useful for admin overview.

```
┌─────────────────────────────────────────────────────────────────┐
│ All Jobs                                    [▶ Run New Job]      │
│                                                                 │
│ [All] [Running] [Analyzed] [Completed] [Failed]                 │
│                                                                 │
│ Project          Job ID      Status     Docs  Created    Duration│
│ Elmer breach     bcb976d6    analyzed   1     4 hrs ago  --     │
│ Client XYZ       76d841a8    running    1     2 days ago --     │
│ ...                                                             │
└─────────────────────────────────────────────────────────────────┘
```

- Same filtering/pagination as project-level Jobs tab
- Shows project name column (since this is cross-project)
- "Run New Job" requires project selection (dropdown)

**Settings page — `frontend/src/pages/Settings.tsx`** (new):

```
┌─────────────────────────────────────────────────────────────────┐
│ Settings                                                        │
│                                                                 │
│ Application                                                     │
│   App Name: Forentis AI                                         │
│   Version: 1.0.0                                                │
│   Database: PostgreSQL (connected)                              │
│                                                                 │
│ LLM Configuration                                               │
│   Status: ✅ Connected (Ollama at localhost:11434)               │
│   Model: qwen2.5:7b                                             │
│   LLM Assist: Enabled / Disabled [toggle]                       │
│   PII Masking: Enabled [toggle]                                 │
│                                                                 │
│ Data Locality                                                   │
│   Inference Endpoint: http://localhost:11434                     │
│   Endpoint Verified: ✅ Local (127.0.0.1)                       │
│   Network Isolated: ✅                                           │
│                                                                 │
│ Diagnostics                                                     │
│   [Run System Check]  [View Audit Log]  [Export Config]         │
└─────────────────────────────────────────────────────────────────┘
```

- Absorbs the old Diagnostic page functionality
- Shows LLM connection status (calls `GET /api/health` or similar)
- Data locality info from the roadmap addendum
- "Run System Check" replaces the old Diagnostic page
- Read-only for now (no editing config via UI — that's a future feature)

**Route changes (`App.tsx`):**

| Old route | New route | Notes |
|---|---|---|
| `/` (Dashboard) | `/` (Dashboard) | Redesigned |
| `/projects` | `/projects` | Unchanged |
| `/projects/:id` | `/projects/:id` | Unchanged |
| `/low-confidence` | REMOVED | Merged into /review |
| `/escalation` | REMOVED | Merged into /review |
| `/qc-sampling` | REMOVED | Merged into /review |
| `/rra-review` | REMOVED | Merged into /review |
| — | `/review` | NEW: merged review queue |
| `/submit-job` | `/jobs` | Renamed + reworked |
| `/diagnostic` | REMOVED | Absorbed into /settings |
| — | `/settings` | NEW: settings page |

---

#### 16d. Density Tab Clarity

**File: `frontend/src/pages/ProjectDetail.tsx`** — DensityTab component.

**State-driven display:**

```
STATE 1 — No extraction completed yet:
  ┌─────────────────────────────────────────────────┐
  │  📊 Density Analysis                            │
  │                                                 │
  │  No density data available yet.                 │
  │                                                 │
  │  Density analysis runs automatically after      │
  │  document extraction (Phase 2) completes.       │
  │                                                 │
  │  Current status: 1 document analyzed,           │
  │  awaiting review and extraction.                │
  │                                                 │
  │  [Go to Jobs tab to review and extract →]       │
  └─────────────────────────────────────────────────┘

STATE 2 — Extraction completed, density available:
  ┌─────────────────────────────────────────────────┐
  │  📊 Density Analysis                            │
  │                                                 │
  │  ┌────────┐ ┌────────┐ ┌────────┐ ┌──────────┐ │
  │  │ 450    │ │ 380    │ │ 120    │ │ High     │ │
  │  │ Total  │ │ PII    │ │ PHI    │ │ Confidence│ │
  │  └────────┘ └────────┘ └────────┘ └──────────┘ │
  │                                                 │
  │  By Category:                                   │
  │  PII ████████████████████ 380 (84%)             │
  │  PHI ████████░░░░░░░░░░░ 120 (27%)             │
  │  PFI ███░░░░░░░░░░░░░░░░  45 (10%)             │
  │                                                 │
  │  By Document:                                   │
  │  payroll.pdf      230 entities   High confidence │
  │  insurance.pdf    120 entities   Partial         │
  │  medical.pdf      100 entities   High confidence │
  └─────────────────────────────────────────────────┘
```

- Clear empty state explaining what density is and when it appears
- Link to Jobs tab when extraction hasn't run yet
- Summary cards when data exists
- Visual bars for category breakdown
- Per-document density table

---

#### 16e. Execution Prompts (split into 2 runs)

**Step 16 Part 1 — Dashboard + Jobs Tab (run first):**

```
@agent-general-purpose Read CLAUDE.md and docs/PLAN.md Step 16 for context.

Step 16 Part 1: Dashboard redesign + Jobs tab improvements.

=== AREA 1: DASHBOARD (backend + frontend) ===

1a. Create app/api/routes/dashboard.py:
    GET /dashboard/summary endpoint returning JSON with:
    - stats: active_projects, pending_reviews, jobs_this_week, documents_processed
    - needs_attention: projects with pending document_analysis_reviews
    - running_jobs: ingestion_runs with status in (pending, running, analyzing, extracting)
    - active_projects: projects with status='active', ordered by updated_at DESC, limit 10
      Include: document_count, last_activity_at, pending_reviews count
    - recent_activity: last 20 events (union of job completions, doc approvals,
      export creations, project creations), ordered by timestamp DESC
    Register in app/api/main.py. Add /dashboard proxy in vite.config.ts.

1b. Rewrite frontend/src/pages/Dashboard.tsx:
    - 4 stat cards row (Active Projects, Pending Reviews, Jobs This Week, Docs Processed)
      Clickable: projects → /projects, reviews → first pending project's Jobs tab
    - Needs Attention section: list of projects with pending reviews, [Review] button
      each. Empty state: "All clear — no documents waiting for review"
    - Running Jobs section: progress bars, [View] buttons. Hidden when empty.
      Auto-refresh every 10s via react-query refetchInterval.
    - Active Projects: compact cards, clickable → /projects/:id
      Empty state: "Create your first project" + [New Project] button
    - Recent Activity: timeline feed with icons per event type
    - Overall dashboard polls every 30s, 10s when running jobs exist

=== AREA 2: JOBS TAB IMPROVEMENTS (backend + frontend) ===

2a. Add backend endpoints in app/api/routes/jobs.py:
    - POST /jobs/{id}/cancel — sets status='cancelled', returns updated job
    - DELETE /jobs/{id} — soft delete: sets status='archived'
    - Update GET /projects/{id}/jobs to support query params:
      ?status=analyzed&page=1&per_page=10
      Returns: {jobs: [...], total: N, page: N, per_page: N}

2b. Update GET /projects/{id}/jobs response to include first document
    filename for each job (join ingestion_runs → documents, take first
    document's file_name, truncate to 50 chars).

2c. Update JobsTab in frontend/src/pages/ProjectDetail.tsx:
    - Add "Document" column showing first filename (truncated, tooltip for full)
    - Add red "Cancel" button on running/pending jobs → calls POST /jobs/{id}/cancel
    - Add trash icon on completed/failed/cancelled jobs → calls DELETE /jobs/{id}
      Confirm dialog before delete.
    - Add status filter dropdown above table: All, Running, Analyzed, Completed, Failed
    - Pagination: 10 per page with page controls (Previous / Next / page numbers)
    - Fix duration: show time for analyzed jobs (created_at to analysis_completed_at)
      Format: "2m 15s" for analyzed, "13m 50s" for completed
    - Add "▶ Run New Job" button in tab header (protocol selector inline dropdown)
    - Column header click to sort by date, status, docs, duration

=== TESTING ===

3. Create tests/test_dashboard.py:
   - Empty state: all counts zero
   - With data: correct counts for stats, needs_attention, running_jobs
   - Recent activity ordered by timestamp DESC, limit 20
   - Active projects ordered by last_activity DESC

4. Create tests/test_jobs_management.py:
   - POST /jobs/{id}/cancel sets status to cancelled
   - DELETE /jobs/{id} sets status to archived
   - GET /projects/{id}/jobs with status filter returns correct subset
   - GET /projects/{id}/jobs with pagination returns correct page
   - Job response includes first document filename

5. Run pytest on ALL changed files. Fix failures up to 3 attempts.
   Document any blockers in docs/BLOCKERS.md. Update CLAUDE.md.
```

**Step 16 Part 2 — Sidebar Consolidation + Density Tab (run after Part 1):**

```
@agent-general-purpose Read CLAUDE.md and docs/PLAN.md Step 16 for context.

Step 16 Part 2: Sidebar navigation consolidation + Density tab clarity.

=== AREA 3: SIDEBAR CONSOLIDATION (frontend) ===

3a. Create frontend/src/pages/ReviewQueue.tsx (new):
    Single page merging all 4 review queues.
    - Tab bar: [All] [Low Confidence] [Escalation] [QC Sampling] [RRA Review]
    - Table: Project name, Document name, Queue type badge, Status, Age
    - Clicking row opens review detail (reuse existing review components)
    - "All" tab shows everything sorted by oldest first
    - Badge count on sidebar nav item showing total pending

3b. Create frontend/src/pages/Settings.tsx (new):
    - Application section: app name, version, database status
    - LLM Configuration: status (connected/disconnected), model name,
      LLM assist toggle display, PII masking status
    - Data Locality: inference endpoint, verification status
    - Diagnostics section: absorb old Diagnostic page functionality
      "Run System Check" button, "View Audit Log" link
    Note: all read-only for now. No config editing via UI.

3c. Rework frontend/src/pages/JobSubmit.tsx → Jobs.tsx:
    Global jobs view across all projects.
    - Same table as project Jobs tab but with Project name column
    - Same filters/pagination
    - "Run New Job" button requires project selection (dropdown)

3d. Update frontend/src/App.tsx routes and sidebar:
    REMOVE routes: /low-confidence, /escalation, /qc-sampling, /rra-review, /diagnostic
    ADD routes: /review (ReviewQueue), /settings (Settings)
    RENAME: /submit-job → /jobs (Jobs global view)
    
    New sidebar order (5 items):
    1. Dashboard (icon: LayoutDashboard)
    2. Projects (icon: FolderOpen)
    3. Review Queue (icon: ClipboardCheck, badge: pending count)
    4. Jobs (icon: Play)
    5. Settings (icon: Settings)

=== AREA 4: DENSITY TAB ===

4a. Update DensityTab in frontend/src/pages/ProjectDetail.tsx:
    STATE 1 (no extraction data):
      Show explanation: "Density analysis runs after extraction completes"
      Show current project status (X docs analyzed, Y awaiting review)
      Link: "Go to Jobs tab to review and extract →"
    
    STATE 2 (extraction complete, density available):
      Summary cards: Total Entities, by category counts, confidence level
      Category breakdown with visual bars (PII, PHI, PFI percentages)
      Per-document density table with entity count and confidence

=== TESTING ===

5. Verify all old routes (/low-confidence, /escalation, /qc-sampling,
   /rra-review, /diagnostic) are removed and redirect or 404 cleanly.

6. Verify new routes (/review, /settings, /jobs) render correctly.

7. Verify sidebar shows exactly 5 items in correct order.

8. Run pytest on ALL changed files. Fix failures up to 3 attempts.
   Document any blockers in docs/BLOCKERS.md. Update CLAUDE.md with
   everything done including test counts.
```