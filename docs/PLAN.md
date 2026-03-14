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

---

### Step 17 — Cross-Page Template Linking + FP Cleanup + Auto-Export (PENDING)

**Goal:** Handle real-world multi-page documents where one individual's PII spans multiple pages. The LLM reads enough pages to detect repeating templates, groups pages into per-individual records, and feeds pre-grouped data to entity resolution. Also fix remaining false positive patterns (financial terms as PERSON) and auto-generate exports after job completion.

**Observed problem (Boosey & Hawkes Pension Transfer — multi-individual, multi-page):**

A 6-page PDF with 2 individuals, 3 pages each:
```
Pages 1-3: K P Acheampong
  Page 1: Statement of Entitlement (name, scheme name, transfer values £5170)
  Page 2: Member Details (name, address 85 Waltings Gardens London NW2 3UD, DOB 10-Aug-1959, NI NE724362D)
  Page 3: Benefit Components (pension £861.31, lump sum, spouse/dependant details)

Pages 4-6: M S Alcock
  Page 4: Same template as page 1 (transfer values £16713)
  Page 5: Same template as page 2 (address 3 Whitworth Road Wellingborough, DOB 29-Aug-1960, NI WK393925C)
  Page 6: Same template as page 3
```

**Current failures:**
1. Page-by-page extraction creates isolated PIIRecords — "K P Acheampong" on page 1 is never linked to "NE724362D" on page 2
2. EntityResolver gets records with only one raw_* field each → all merge confidence = 1.0 (each record is its own group) → no cross-page merging
3. "Lump Sum", "Harrow Weald", "Buckman", "Gallick" classified as PERSON (financial terms and location names)
4. "Cole WI726762D" — name + NI number concatenated (extraction boundary error)
5. CSV export shows 20+ garbage rows instead of 2 clean individuals
6. No auto-export after job completion — user must manually trigger

---

#### 17a. Document Template Detection

**Extension to `app/structure/document_schema.py`:**

```python
@dataclass
class PageRole:
    page_offset: int            # 0, 1, 2 within template
    role: str                   # "financial_summary", "member_details", "benefit_details"
    pii_fields_expected: list[str]  # ["PERSON", "LOCATION", "DATE_OF_BIRTH", "NI_NUMBER"]
    is_identity_page: bool      # True if this page has the primary subject name

@dataclass
class DocumentTemplate:
    """Describes a repeating multi-page template within a document."""
    template_name: str              # "pension_transfer_statement"
    pages_per_instance: int         # 3
    total_instances_estimate: int   # 2 (based on total pages / pages_per_instance)
    page_roles: list[PageRole]
    identity_page_offset: int       # 0 — which page within the template has the name
    
    # For extraction: group pages into instances
    def get_instance_pages(self, total_pages: int) -> list[list[int]]:
        """Returns [[0,1,2], [3,4,5]] for a 6-page doc with 3-page template."""
        instances = []
        for start in range(0, total_pages, self.pages_per_instance):
            end = min(start + self.pages_per_instance, total_pages)
            instances.append(list(range(start, end)))
        return instances

@dataclass
class DocumentSchema:
    # ... all existing fields ...
    template: DocumentTemplate | None = None  # NEW — for repeating multi-page documents
```

#### 17b. Configurable LLM Page Reading per Protocol

**Extension to `app/core/constants.py`:**

```python
PROTOCOL_LLM_CONFIG = {
    "hipaa": {
        "llm_pages_to_read": 5,        # medical records can have long headers
        "expect_multi_page_records": True,
    },
    "gdpr": {
        "llm_pages_to_read": 3,
        "expect_multi_page_records": True,
    },
    "state_breach": {
        "llm_pages_to_read": 3,
        "expect_multi_page_records": True,
    },
    "pci_dss": {
        "llm_pages_to_read": 2,        # card data usually on 1-2 pages
        "expect_multi_page_records": False,
    },
    "dpdpa": {
        "llm_pages_to_read": 3,
        "expect_multi_page_records": True,
    },
    "bipa": {
        "llm_pages_to_read": 2,
        "expect_multi_page_records": False,
    },
    "ferpa": {
        "llm_pages_to_read": 4,        # student records can be multi-page
        "expect_multi_page_records": True,
    },
    "ccpa": {
        "llm_pages_to_read": 3,
        "expect_multi_page_records": True,
    },
}
```

Also extend `protocol_configs.config_json`:

```json
{
  "target_entity_types": [...],
  "llm_pages_to_read": 6,
  "expect_multi_page_records": true,
  "...existing fields..."
}
```

User-configured value overrides protocol default. Fallback to protocol default. Fallback to 3 pages if no protocol.

#### 17c. LLM Multi-Page Understanding Prompt

**Update `app/llm/prompts.py`** — Extend `UNDERSTAND_DOCUMENT` template or add `UNDERSTAND_MULTI_PAGE_DOCUMENT`:

The LLM now reads **N pages** (configurable) instead of just the onset page:

```
You are analyzing a multi-page document to understand its structure.

Document: {file_name} ({file_type}, {total_pages} pages)
Protocol: {protocol_name}

--- PAGE 1 ---
{page_1_text}
--- PAGE 2 ---
{page_2_text}
--- PAGE 3 ---
{page_3_text}
[... up to N pages ...]

Analyze this document and determine:

1. Is this a REPEATING TEMPLATE document where the same form repeats
   for multiple individuals? If yes, identify:
   - How many pages per individual
   - Which page within the template has the person's name
   - What PII fields appear on each page

2. For each page, identify fields and their types (same as before).

Respond ONLY with JSON:
{
  "document_type": "...",
  "template": {
    "is_repeating": true/false,
    "template_name": "descriptive name",
    "pages_per_instance": N,
    "total_instances_estimate": N,
    "page_roles": [
      {
        "page_offset": 0,
        "role": "what this page contains",
        "pii_fields_expected": ["PERSON", "LOCATION", ...],
        "is_identity_page": true/false
      }
    ]
  },
  "field_map": [...],
  "people": [...],
  "tables": [...],
  "suppression_hints": [...]
}

IMPORTANT:
- Financial terms (Lump Sum, Transfer Value, Premium, Pension) are NOT person names
- Location names (Harrow Weald, Buckman, Gallick) should be LOCATION, not PERSON
- If a value contains both a name and an ID number concatenated (e.g., "Cole WI726762D"),
  these are SEPARATE entities that should be extracted independently
- For repeating templates, describe the PATTERN (pages_per_instance), not every instance
```

#### 17d. Template-Aware Extraction Grouping

**File: `app/pipeline/two_phase.py`** — Modify `extract_generator()`:

When DocumentSchema has a `template` field with `is_repeating=True`:

```python
def extract_with_template(doc, schema, detections_by_page):
    """
    Group extractions by template instance, not just by page.
    
    For a 6-page doc with 3-page template:
      Instance 1 (pages 0-2): all detections → one PIIRecord set
        raw_name from page 0 (identity page)
        raw_address from page 1
        raw_dob from page 1
        raw_government_id from page 1 (NI number)
      Instance 2 (pages 3-5): all detections → another PIIRecord set
        raw_name from page 3
        raw_address from page 4
        ...
    """
    template = schema.template
    instances = template.get_instance_pages(doc.total_pages)
    
    grouped_records = []
    for instance_pages in instances:
        # Collect all detections across pages in this instance
        instance_detections = []
        for page in instance_pages:
            instance_detections.extend(detections_by_page.get(page, []))
        
        # Build ONE composite PIIRecord per instance
        # (merges name from page 0 + address from page 1 + DOB from page 1)
        composite_record = build_composite_record(instance_detections, doc.id)
        grouped_records.append(composite_record)
    
    return grouped_records

def build_composite_record(detections, doc_id):
    """
    Merge multiple detections into a single PIIRecord with all raw_* fields populated.
    Takes the highest-confidence detection for each field type.
    """
    rec = PIIRecord(
        record_id=str(uuid4()),
        source_document_id=str(doc_id),
    )
    
    # Group detections by semantic field type
    names = [d for d in detections if d.entity_type in PERSON_TYPES]
    emails = [d for d in detections if d.entity_type in EMAIL_TYPES]
    phones = [d for d in detections if d.entity_type in PHONE_TYPES]
    dobs = [d for d in detections if d.entity_type in DOB_TYPES]
    addresses = [d for d in detections if d.entity_type in ADDRESS_TYPES]
    gov_ids = [d for d in detections if d.entity_type in GOV_ID_TYPES]
    
    # Take highest-confidence detection for each field
    if names:
        best = max(names, key=lambda d: d.confidence)
        rec.raw_name = extract_text(best)
        rec.entity_type = best.entity_type
    if emails:
        rec.raw_email = extract_text(max(emails, key=lambda d: d.confidence))
    if phones:
        rec.raw_phone = extract_text(max(phones, key=lambda d: d.confidence))
    if dobs:
        rec.raw_dob = extract_text(max(dobs, key=lambda d: d.confidence))
    if addresses:
        rec.raw_address = extract_text(max(addresses, key=lambda d: d.confidence))
    if gov_ids:
        rec.raw_government_id = extract_text(max(gov_ids, key=lambda d: d.confidence))
    
    # Store all PII types found across all pages
    rec.pii_types_found = list(set(d.entity_type for d in detections))
    rec.page_range = f"{detections[0].block.page_or_sheet}-{detections[-1].block.page_or_sheet}"
    
    return rec
```

**Without template (single-page or no LLM):** Falls back to existing per-detection PIIRecord creation (the record_mapper fix already applied).

**With template:** Produces one rich PIIRecord per individual with all fields populated from across their pages. EntityResolver then has real data to work with.

**Expected result for the pension document:**

```
Without template (current):
  20+ PIIRecords, each with 1 field → 20+ NotificationSubjects → garbage CSV

With template:
  2 PIIRecords:
    Record 1: raw_name="K P Acheampong", raw_address="85 Waltings Gardens, London NW2 3UD",
              raw_dob="10-Aug-1959", raw_government_id="NE724362D"
    Record 2: raw_name="M S Alcock", raw_address="3 Whitworth Road, Wellingborough, Northants NN8 1QQ",
              raw_dob="29-Aug-1960", raw_government_id="WK393925C"
  → 2 NotificationSubjects → clean CSV with 2 rows
```

#### 17e. Enhanced False Positive Suppression for Financial Documents

**Add to `app/pii/context_deny_list.py`:**

```python
FINANCIAL_TERM_DENY_LIST = frozenset({
    "lump sum", "transfer value", "pension", "premium", "annuity",
    "retirement", "benefit", "contribution", "entitlement", "remuneration",
    "scheme", "fund", "allowance", "deduction", "commission",
    "qualifying", "protected rights", "pensionable", "guaranteed minimum",
    "final remuneration", "basic scheme", "buy-out", "buy out",
})

UK_LOCATION_DENY_FOR_PERSON = frozenset({
    "harrow weald", "buckman", "gallick", "alford",  # Add as encountered
    # More systematic: any LOCATION detection should suppress PERSON for same text
})
```

**Better approach — cross-type suppression rule:**

If the same text is detected as both LOCATION (85%) and PERSON (85%), and the text matches a known geographic pattern or doesn't match a name pattern (no title, no first+last structure), suppress the PERSON detection. Add to `schema_filter.py`:

```python
def cross_type_suppression(detections):
    """
    If same text is detected as both PERSON and LOCATION/ORGANIZATION,
    apply heuristic to keep the more likely one.
    
    Rules:
    - Has title (Mr, Mrs, Dr) → keep PERSON
    - Has comma-separated multi-word → likely LOCATION
    - Is a single word matching UK postcode area → LOCATION
    - Contains digits → NOT a PERSON
    - Is a well-known financial term → NOT a PERSON
    """
```

#### 17f. Auto-Export After Job Completion

**File: `app/pipeline/two_phase.py`** — At end of `extract_generator()`, after notification stage completes:

```python
# After notification list is built, auto-generate CSV export
if notification_subjects:
    from app.export.csv_exporter import CSVExporter
    exporter = CSVExporter(db_session)
    export_job = exporter.run(
        project_id=project_id,
        protocol_config_id=protocol_config_id,
        output_dir=settings.export_dir,
    )
    yield sse_event("export", "complete", f"CSV export ready: {export_job.row_count} subjects")
```

**File: `app/api/routes/jobs.py`** — Same for `_pipeline_generator()`.

**Frontend:** Exports tab should auto-refresh when a job completes. Show most recent export at the top with download link.

#### 17g. Execution Prompts (split into 2 runs)

**Step 17 Part 1 — Template detection + cross-page grouping (run first):**

```
@agent-general-purpose Read CLAUDE.md and docs/PLAN.md Step 17 for context.

Step 17 Part 1: Multi-page template detection and cross-page PII grouping.

1. Add DocumentTemplate, PageRole dataclasses to app/structure/document_schema.py
   (exact definitions in PLAN.md Step 17a). Add template field to DocumentSchema.

2. Add PROTOCOL_LLM_CONFIG dict to app/core/constants.py with llm_pages_to_read
   and expect_multi_page_records per protocol (8 protocols).
   See PLAN.md Step 17b for exact values.

3. Extend protocol_configs.config_json schema to accept llm_pages_to_read
   and expect_multi_page_records (optional, overrides protocol defaults).

4. Update app/structure/llm_document_understanding.py:
   - Read N pages (from protocol config → protocol default → 3) instead of just onset page
   - Send multi-page text to LLM with UNDERSTAND_MULTI_PAGE_DOCUMENT prompt
   - Parse template field from LLM response into DocumentTemplate
   - If template.is_repeating, store on DocumentSchema

5. Update or add UNDERSTAND_MULTI_PAGE_DOCUMENT prompt to app/llm/prompts.py
   (see PLAN.md Step 17c for exact prompt). Instructs LLM to detect repeating
   templates, identify pages_per_instance, page roles, and identity page.

6. Create app/pipeline/record_mapper.py (or extend existing):
   - build_composite_record(detections, doc_id) → single PIIRecord with all
     raw_* fields populated from multiple detections
   - extract_with_template(doc, schema, detections_by_page) → list of composite
     PIIRecords, one per template instance (groups pages by instance)

7. Update app/pipeline/two_phase.py extract_generator():
   - If DocumentSchema has template with is_repeating=True:
     use extract_with_template() to group pages → composite records
   - If no template: use existing per-detection record_mapper (from PIIRecord fix)
   - Pass composite records to EntityResolver

8. Create tests/test_template_detection.py:
   - DocumentTemplate.get_instance_pages: 6 pages / 3 per = [[0,1,2],[3,4,5]]
   - build_composite_record: merges PERSON + LOCATION + DOB from different pages
   - Composite record has raw_name + raw_address + raw_dob all populated
   - Without template: falls back to per-detection records
   - Pension doc mock: 2 instances → 2 composite records → 2 NotificationSubjects

9. Run pytest on all changed files. Fix failures up to 3 attempts.
   Update CLAUDE.md.
```

**Step 17 Part 2 — FP cleanup + auto-export (run after Part 1):**

```
@agent-general-purpose Read CLAUDE.md and docs/PLAN.md Step 17 for context.

Step 17 Part 2: Financial term FP suppression, cross-type suppression, auto-export.

1. Add FINANCIAL_TERM_DENY_LIST to app/pii/context_deny_list.py:
   frozenset of financial terms that should never be classified as PERSON
   (lump sum, transfer value, pension, premium, annuity, remuneration,
   contribution, entitlement, protected rights, etc.)
   Update is_likely_false_positive() to check FINANCIAL_TERM_DENY_LIST
   when entity_type is PERSON.

2. Add cross_type_suppression() to app/pii/schema_filter.py (or context_deny_list.py):
   When same text is detected as both PERSON and LOCATION:
   - If text has title (Mr/Mrs/Dr) → keep PERSON
   - If text is a single common word or matches location pattern → keep LOCATION
   - If text contains digits → suppress PERSON
   - If text matches FINANCIAL_TERM_DENY_LIST → suppress PERSON
   Apply before other filters in the detection pipeline.

3. Add extraction boundary fix:
   When detected text contains both a name and an ID pattern concatenated
   (e.g., "Cole WI726762D"), split into separate detections:
   - "Cole" → PERSON
   - "WI726762D" → NI_NUMBER / government ID
   Add as a post-processing step in detection pipeline.

4. Wire auto-export into pipeline completion:
   In app/pipeline/two_phase.py extract_generator(), after notification
   stage completes, auto-generate CSV export via CSVExporter.run().
   Emit SSE event: {"stage": "export", "status": "complete"}.
   Same in app/api/routes/jobs.py _pipeline_generator().

5. Update frontend Exports tab in ProjectDetail.tsx:
   Auto-refresh when job completes (invalidate react-query cache).
   Show most recent export at top with prominent download link.
   Add "Auto-generated" badge on exports created by pipeline (vs manual).

6. Create tests/test_financial_fp.py:
   - "Lump Sum" → PERSON suppressed by FINANCIAL_TERM_DENY_LIST
   - "Harrow Weald" → PERSON suppressed when also detected as LOCATION
   - "Mr K P Acheampong" → PERSON kept (has title)
   - "Cole WI726762D" → split into PERSON "Cole" + NI_NUMBER "WI726762D"
   - Auto-export: after pipeline completes, ExportJob exists with status=completed

7. Run pytest on all changed files. Fix failures up to 3 attempts.
   Update CLAUDE.md.
```

---

### Step 18 — Auditor-Ready CSV Export with Lineage (PENDING)

**Goal:** Restructure the CSV export from a fragmented per-detection format to a clean per-individual format that auditors and law firms can actually use. Each row = one affected individual. PII types are columns, not row values. Every row includes source document, page range, and confidence for full audit lineage.

**Current export (broken for auditors):**

```csv
canonical_name,canonical_email,canonical_phone,pii_types_found,merge_confidence,review_status
,,,"[""LOCATION""]",1.0000,AI_PENDING
Lump Sum,,,"[""PERSON""]",1.0000,AI_PENDING
Buckman,,,"[""PERSON""]",1.0000,AI_PENDING
```

Problems:
- One row per detection, not per individual
- PII types as a JSON array in one column instead of separate columns
- No source document or page reference
- No address, DOB, or government ID columns
- Includes false positives ("Lump Sum", "Buckman")
- LOCATION rows have no name — useless standalone

**Target export (what law firms need):**

```csv
individual_id,name,address,date_of_birth,government_id,government_id_type,email,phone,pii_types_found,source_document,source_pages,extraction_confidence,merge_confidence,review_status,notification_required
1,K P Acheampong,"85 Waltings Gardens, Shoot Up Hill, London NW2 3UD",10-Aug-1959,NE724362D,NI_NUMBER,,,PERSON|LOCATION|DATE_OF_BIRTH|NI_NUMBER,pension_transfer.pdf,1-3,0.92,0.92,APPROVED,true
2,M S Alcock,"3 Whitworth Road, Wellingborough, Northants NN8 1QQ",29-Aug-1960,WK393925C,NI_NUMBER,,,PERSON|LOCATION|DATE_OF_BIRTH|NI_NUMBER,pension_transfer.pdf,4-6,0.95,0.95,APPROVED,true
```

Properties:
- One row per unique individual (NotificationSubject)
- Separate columns for each PII type (name, address, DOB, gov ID, email, phone)
- Government ID has both value and type columns (SSN vs NI_NUMBER vs Aadhaar)
- Source document filename for lineage
- Page range showing which pages this individual's data came from
- Pipe-delimited PII types found (not JSON array)
- Both extraction and merge confidence
- Notification required flag (per protocol)

---

#### 18a. Export Schema Definition

**File: `app/export/export_schema.py`** (new):

```python
from dataclasses import dataclass, field

@dataclass
class ExportColumn:
    name: str               # column header
    source_field: str       # where to get data from NotificationSubject/PIIRecord
    mask_strategy: str      # "none", "mask_email", "mask_phone", "mask_gov_id", "mask_address"
    required: bool          # must have value for row to be valid

# Default export columns — auditor-friendly
AUDITOR_EXPORT_COLUMNS = [
    ExportColumn("individual_id",       "row_number",           "none",         True),
    ExportColumn("name",                "canonical_name",       "none",         True),
    ExportColumn("address",             "canonical_address",    "mask_address", False),
    ExportColumn("date_of_birth",       "raw_dob",              "none",         False),
    ExportColumn("government_id",       "raw_government_id",    "mask_gov_id",  False),
    ExportColumn("government_id_type",  "government_id_type",   "none",         False),
    ExportColumn("email",               "canonical_email",      "mask_email",   False),
    ExportColumn("phone",               "canonical_phone",      "mask_phone",   False),
    ExportColumn("pii_types_found",     "pii_types_found",      "none",         False),
    ExportColumn("source_document",     "source_document_name", "none",         True),
    ExportColumn("source_pages",        "source_page_range",    "none",         False),
    ExportColumn("extraction_confidence","extraction_confidence","none",         False),
    ExportColumn("merge_confidence",    "merge_confidence",     "none",         False),
    ExportColumn("review_status",       "review_status",        "none",         True),
    ExportColumn("notification_required","notification_required","none",         False),
]

# Minimal export — just names and notification status
MINIMAL_EXPORT_COLUMNS = [
    ExportColumn("name",                "canonical_name",       "none",         True),
    ExportColumn("notification_required","notification_required","none",         False),
    ExportColumn("review_status",       "review_status",        "none",         True),
]

# Full export — includes raw values (INVESTIGATION mode only)
FULL_EXPORT_COLUMNS = AUDITOR_EXPORT_COLUMNS + [
    ExportColumn("raw_name",            "raw_name",             "none",         False),
    ExportColumn("raw_email",           "raw_email",            "none",         False),
    ExportColumn("raw_phone",           "raw_phone",            "none",         False),
    ExportColumn("raw_address",         "raw_address",          "none",         False),
]

EXPORT_SCHEMAS = {
    "auditor": AUDITOR_EXPORT_COLUMNS,
    "minimal": MINIMAL_EXPORT_COLUMNS,
    "full": FULL_EXPORT_COLUMNS,      # only available in INVESTIGATION storage mode
}
```

#### 18b. Lineage Tracking

The lineage must trace each exported row back to its source. Two levels:

**Level 1 — Document lineage** (which file):

```python
# Already available via NotificationSubject → source_document_id → Document.file_name
# Need to populate source_document_name on the export row
```

**Level 2 — Page lineage** (which pages within the file):

```python
# For template-grouped records: stored in PIIRecord.page_range ("1-3")
# For single-page records: stored in PIIRecord.page_or_sheet (1)
# Need to propagate through to NotificationSubject
```

**File: `app/db/models.py`** — Extend NotificationSubject:

```python
# Add to NotificationSubject (or use existing columns if available)
source_document_name = Column(VARCHAR(512), nullable=True)   # "pension_transfer.pdf"
source_page_range = Column(VARCHAR(64), nullable=True)       # "1-3" or "1"
government_id_type = Column(VARCHAR(64), nullable=True)      # "US_SSN", "NI_NUMBER", "AADHAAR"
extraction_confidence = Column(Float, nullable=True)         # avg confidence across detections
pii_types_list = Column(VARCHAR(1024), nullable=True)        # "PERSON|LOCATION|DATE_OF_BIRTH|NI_NUMBER"
```

**Migration 0010** for the new columns.

**File: `app/rra/deduplicator.py`** — Update `build_subjects()`:

When creating NotificationSubject from resolved PIIRecords, populate:
- `source_document_name` from Document.file_name
- `source_page_range` from PIIRecord.page_range or page_or_sheet
- `government_id_type` from the entity_type of the raw_government_id detection
- `extraction_confidence` from average confidence of constituent detections
- `pii_types_list` from pipe-joined unique entity types

#### 18c. Rewrite CSV Exporter

**File: `app/export/csv_exporter.py`** — Major refactor:

```python
class CSVExporter:
    def run(self, project_id, *, protocol_config_id=None, output_dir=None,
            export_schema="auditor", storage_policy="strict"):
        """
        Export NotificationSubjects as auditor-ready CSV.
        
        export_schema: "auditor" (default), "minimal", "full"
        "full" only available when storage_policy="investigation"
        """
        # Get export columns based on schema
        columns = EXPORT_SCHEMAS[export_schema]
        
        # "full" schema requires INVESTIGATION mode
        if export_schema == "full" and storage_policy != "investigation":
            raise ValueError("Full export requires INVESTIGATION storage policy")
        
        # Query subjects with document join for lineage
        subjects = (
            db_session.query(NotificationSubject)
            .filter(NotificationSubject.project_id == project_id)
            .join(Document, ...)  # for source_document_name
            .order_by(NotificationSubject.canonical_name)
            .all()
        )
        
        # Build rows
        rows = []
        for idx, subject in enumerate(subjects, 1):
            row = {}
            for col in columns:
                value = self._get_field_value(subject, col, row_number=idx)
                value = self._apply_mask(value, col.mask_strategy)
                row[col.name] = value
            rows.append(row)
        
        # Write CSV
        content = self._build_csv(columns, rows)
        ...
    
    def _get_field_value(self, subject, col, row_number=None):
        """Map export column to actual data from subject."""
        if col.source_field == "row_number":
            return row_number
        elif col.source_field == "pii_types_found":
            return subject.pii_types_list or ""
        elif col.source_field == "source_document_name":
            return subject.source_document_name or ""
        elif col.source_field == "source_page_range":
            return subject.source_page_range or ""
        # ... etc for each field
    
    def _apply_mask(self, value, strategy):
        """Apply masking strategy based on storage policy."""
        if strategy == "none" or value is None:
            return value
        elif strategy == "mask_email":
            return _mask_email(value)
        elif strategy == "mask_phone":
            return _mask_phone(value)
        elif strategy == "mask_gov_id":
            return _mask_gov_id(value)  # "NE7****2D" — first 3 + last 2 visible
        elif strategy == "mask_address":
            return _mask_address(value)  # state/county + postcode only
```

**New masking function for government IDs:**

```python
def _mask_gov_id(value: str) -> str:
    """Mask government ID: show first 3 and last 2 chars.
    NE724362D → NE7****2D
    285-07-5085 → 285-**-*085
    """
    if not value or len(value) < 5:
        return "***"
    return value[:3] + "*" * (len(value) - 5) + value[-2:]
```

#### 18d. Export Format Options

The protocol config should control export format:

```json
{
  "export_schema": "auditor",
  "export_fields": ["name", "address", "government_id", "source_document"],
  "mask_government_ids": true,
  "include_lineage": true
}
```

- `export_schema` selects the column set (auditor/minimal/full)
- `export_fields` optionally limits which columns appear (subset of schema)
- `mask_government_ids` controls whether gov IDs are masked (default true in STRICT)
- `include_lineage` controls source_document and source_pages columns

#### 18e. Frontend: Exports Tab Enhancement

**File: `frontend/src/pages/ProjectDetail.tsx`** — ExportsTab:

```
┌─────────────────────────────────────────────────────────────┐
│ Exports                                                     │
│                                                             │
│ Latest Export                                                │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ 📄 export_c418511c.csv          Auto-generated          │ │
│ │ 2 individuals • 8 PII fields • Auditor format           │ │
│ │ Created: 2 minutes ago                                  │ │
│ │                                                         │ │
│ │ [⬇ Download CSV]  [👁 Preview]  [📊 Summary]            │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│ Preview:                                                    │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ # │ Name           │ Address        │ DOB        │ Gov ID│ │
│ │ 1 │ K P Acheampong │ ...London NW2  │ 10-Aug-1959│ NE7**│ │
│ │ 2 │ M S Alcock     │ ...NN8 1QQ     │ 29-Aug-1960│ WK3**│ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│ Generate New Export                                         │
│ Schema: [Auditor ▾]  Fields: [All ▾]  [Generate]           │
│                                                             │
│ Export History                                               │
│ │ export_c418511c.csv  Auditor  2 rows  Auto  2 min ago   │ │
│ │ export_1a976d0a.csv  Auditor  0 rows  Manual  4 hrs ago │ │
└─────────────────────────────────────────────────────────────┘
```

Key features:
- Latest export prominently displayed with download button
- Preview table showing first 5 rows inline (no download needed)
- Summary: individual count, PII field count, format
- Schema selector for generating new exports
- Export history with row counts and auto/manual badge
- "Auto-generated" badge for exports created by pipeline completion

#### 18f. Execution Prompt

```
@agent-general-purpose Read CLAUDE.md and docs/PLAN.md Step 18 for context.

Step 18: Auditor-ready CSV export with lineage tracking.

=== SCHEMA + MIGRATION ===

1. Create app/export/export_schema.py:
   - ExportColumn dataclass (name, source_field, mask_strategy, required)
   - AUDITOR_EXPORT_COLUMNS: 15 columns (individual_id, name, address,
     date_of_birth, government_id, government_id_type, email, phone,
     pii_types_found, source_document, source_pages, extraction_confidence,
     merge_confidence, review_status, notification_required)
   - MINIMAL_EXPORT_COLUMNS: 3 columns (name, notification_required, review_status)
   - FULL_EXPORT_COLUMNS: auditor + raw_* fields (INVESTIGATION mode only)
   - EXPORT_SCHEMAS dict mapping "auditor"/"minimal"/"full" to column lists

2. Extend NotificationSubject in app/db/models.py with new nullable columns:
   source_document_name (VARCHAR 512), source_page_range (VARCHAR 64),
   government_id_type (VARCHAR 64), extraction_confidence (Float),
   pii_types_list (VARCHAR 1024).

3. Create migration 0010 for the new NotificationSubject columns.

=== LINEAGE POPULATION ===

4. Update app/rra/deduplicator.py build_subjects():
   When creating NotificationSubject from resolved PIIRecords, populate:
   - source_document_name from Document.file_name (join via source_document_id)
   - source_page_range from PIIRecord.page_range or str(page_or_sheet)
   - government_id_type from entity_type of the raw_government_id detection
   - extraction_confidence from average confidence of constituent detections
   - pii_types_list from pipe-joined unique entity types (e.g., "PERSON|LOCATION|DATE_OF_BIRTH")

5. Update app/pipeline/record_mapper.py build_composite_record():
   Store page_range on the PIIRecord (e.g., "1-3" for template instances).
   Store all entity types found as a list on the record.

=== EXPORT REWRITE ===

6. Rewrite app/export/csv_exporter.py:
   - Accept export_schema parameter ("auditor" default, "minimal", "full")
   - "full" schema raises error if storage_policy is not "investigation"
   - Query NotificationSubjects with Document join for source_document_name
   - Order by canonical_name
   - Build rows using ExportColumn definitions from export_schema.py
   - Apply masking per column strategy:
     mask_email: "***@***.***"
     mask_phone: "***-***-{last4}"
     mask_gov_id: first 3 chars + asterisks + last 2 chars (NE724362D → NE7****2D)
     mask_address: county/state + postcode only
   - Add _mask_gov_id() function
   - Write CSV with proper column headers
   - individual_id is auto-incrementing row number, not a UUID

7. Update auto-export in app/pipeline/two_phase.py:
   Pass export_schema="auditor" to CSVExporter.run() for auto-generated exports.

=== FRONTEND ===

8. Update ExportsTab in frontend/src/pages/ProjectDetail.tsx:
   - Latest export card: filename, row count, PII field count, format badge,
     auto/manual badge, download button
   - Preview table: first 5 rows inline with column headers
     Truncate long values (address > 30 chars). Tooltip for full value.
   - Generate New Export: schema dropdown (Auditor/Minimal/Full),
     generate button. Full only shown if INVESTIGATION mode.
   - Export history: table of past exports with row count, format, age

9. Add export preview endpoint:
   GET /projects/{id}/exports/{eid}/preview?rows=5
   Returns first N rows as JSON (for inline preview without downloading).

=== TESTS ===

10. Create tests/test_export_v2.py:
    - Auditor schema: 15 columns in correct order
    - Row per individual: 2 subjects → 2 CSV rows
    - Lineage: source_document_name populated from Document.file_name
    - Lineage: source_page_range shows "1-3" for template-grouped record
    - Government ID masking: "NE724362D" → "NE7****2D"
    - Government ID type: "NI_NUMBER" column populated
    - PII types: pipe-delimited, not JSON array
    - Email/phone masking: same as existing
    - Address masking: county + postcode only
    - Minimal schema: only 3 columns
    - Full schema: requires INVESTIGATION mode, raises otherwise
    - Auto-export: pipeline completion creates auditor-format export
    - Preview endpoint: returns first 5 rows as JSON
    - Empty project: CSV has headers only, 0 rows

11. Run pytest on ALL changed files. Fix failures up to 3 attempts.
    Update CLAUDE.md.
```

---

### Step 19 — Schema-Driven LLM Extraction for Template Documents (PENDING)

**Goal:** For repeating template documents (pension statements, payroll, HR forms), use the LLM as the primary extractor instead of Presidio. The LLM reads each template instance's pages and extracts structured fields directly — because it can understand "Date of Birth: 10-Aug-1959" in context, which Presidio cannot. The extraction prompt is generated from the DocumentSchema, not hardcoded. Presidio remains the primary extractor for non-template documents.

**Why this is needed:**

Presidio fundamentally cannot extract:
- UK dates: "10-Aug-1959" → no DATE_OF_BIRTH recognizer for DD-MMM-YYYY
- UK NI numbers: "NE724362D" → no recognizer exists (pattern: 2 letters + 6 digits + 1 letter)
- Full structured addresses: "85 Waltings Gardens, Shoot Up Hill, London NW2 3UD" → only "London" detected as LOCATION
- DOB from labeled fields: "2.3 Date of Birth: 10-Aug-1959" → Presidio sees a date but doesn't know it's a DOB

The LLM reads the page and understands that "2.3 Date of Birth:" is a label and "10-Aug-1959" is the value. No regex pattern needed.

**Architecture — schema-driven, not hardcoded:**

```
DocumentSchema (from Step 14b, already exists)
      │
      ▼
Prompt Generator (NEW — builds prompt from schema)
      │
      ├── Template documents → LLM extracts per-instance → Presidio validates
      │
      └── Non-template documents → Presidio extracts per-page → Schema filters
```

The extraction prompt is GENERATED from `schema.template.page_roles[].pii_fields_expected`. Different document types produce different schemas, which produce different prompts. Zero hardcoding.

---

#### 19a. LLM Extraction Prompt Generator

**File: `app/llm/extraction_prompts.py`** (new):

```python
def build_extraction_prompt(
    page_texts: list[str],        # text from each page in this template instance
    page_roles: list[PageRole],   # from DocumentSchema.template
    instance_index: int,          # which instance (0-based)
    document_type: str,           # from DocumentSchema
) -> str:
    """
    Build an LLM extraction prompt from the DocumentSchema.
    
    The prompt tells the LLM:
    1. What type of document this is
    2. How many pages to read
    3. What fields to extract (from page_roles.pii_fields_expected)
    4. Expected format for each field type
    
    Returns a prompt string. NOT hardcoded to any document type.
    """
    # Collect all expected PII fields across all pages
    all_fields = set()
    for role in page_roles:
        all_fields.update(role.pii_fields_expected)
    
    # Map entity types to human-readable extraction instructions
    field_instructions = []
    for field in sorted(all_fields):
        instruction = ENTITY_EXTRACTION_GUIDE.get(field, f"Extract any {field} values")
        field_instructions.append(f"- {field}: {instruction}")
    
    prompt = f"""You are extracting personal information from a {document_type}.
This is instance {instance_index + 1}. Read the following pages and extract the requested fields.

{chr(10).join(f'--- PAGE {i+1} ---{chr(10)}{text}{chr(10)}' for i, text in enumerate(page_texts))}

Extract these fields and return ONLY a JSON object:
{{
{chr(10).join(f'  "{field}": "extracted value or null if not found"' for field in sorted(all_fields))}
}}

Field extraction guide:
{chr(10).join(field_instructions)}

RULES:
- Extract the EXACT value as it appears in the document
- If a field is not present on any page, set it to null
- For addresses, include the COMPLETE address (street, area, city, postcode, country)
- For dates, preserve the original format (10-Aug-1959, not 1959-08-10)
- For names, include title if present (Mr, Mrs, Dr)
- Do NOT guess or infer values that are not explicitly stated
"""
    return prompt


# Entity type → extraction instruction mapping
# This is the ONLY "configuration" — it tells the LLM what each field type looks like
# It is NOT document-type-specific
ENTITY_EXTRACTION_GUIDE = {
    "PERSON": "Full name including title (Mr/Mrs/Dr) if present",
    "LOCATION": "Complete address — street, area, city, county, postcode, country. Combine all address lines into one value.",
    "DATE_OF_BIRTH": "Date of birth in original format. Look for labels like 'Date of Birth', 'DOB', 'Born'.",
    "US_SSN": "Social Security Number (XXX-XX-XXXX format)",
    "NI_NUMBER": "UK National Insurance Number (2 letters + 6 digits + 1 letter, e.g., NE724362D). Look for labels like 'National Insurance Number', 'NI No', 'NINO'.",
    "NHS_NUMBER": "UK NHS Number (10 digits, usually formatted XXX XXX XXXX)",
    "EMAIL_ADDRESS": "Email address",
    "PHONE_NUMBER": "Phone number in any format",
    "CREDIT_CARD": "Credit/debit card number",
    "US_DRIVER_LICENSE": "Driver's license number",
    "US_PASSPORT": "Passport number",
    "AADHAAR": "Indian Aadhaar number (12 digits)",
    "PAN_CARD": "Indian PAN (5 letters + 4 digits + 1 letter)",
    "IBAN_CODE": "IBAN bank account number",
    "US_BANK_NUMBER": "Bank account or routing number",
    "MEDICAL_LICENSE": "Medical license or registration number",
    "NPI_NUMBER": "National Provider Identifier (10 digits)",
}
```

**Key: `ENTITY_EXTRACTION_GUIDE` is a generic mapping, not tied to any document type.** The same guide works for pension statements, medical records, HR files, insurance claims — because it describes entity types, not document layouts.

#### 19b. LLM Template Extractor

**File: `app/structure/llm_template_extractor.py`** (new):

```python
class LLMTemplateExtractor:
    """
    Extracts PII from template document instances using LLM.
    
    For each template instance (e.g., pages 1-3 for person 1, pages 4-6 for person 2):
    1. Reads the page texts
    2. Builds an extraction prompt from the DocumentSchema
    3. Sends to LLM (Ollama)
    4. Parses JSON response into a structured record
    5. Validates extracted values against Presidio patterns (optional)
    
    Falls back gracefully: if LLM fails for an instance, returns None
    and the pipeline falls back to Presidio for that instance.
    """
    
    def __init__(self, ollama_client, db_session=None):
        self.client = ollama_client
        self.db_session = db_session
    
    def extract_all_instances(
        self, 
        doc,                      # Document ORM object
        schema: DocumentSchema,   # with template populated
        page_texts: dict[int, str],  # page_number → text
    ) -> list[PIIRecord]:
        """
        Extract PII from all template instances in the document.
        Returns one PIIRecord per instance with all fields populated.
        """
        template = schema.template
        instances = template.get_instance_pages(doc.total_pages or len(page_texts))
        
        records = []
        for idx, instance_pages in enumerate(instances):
            # Get text for this instance's pages
            texts = [page_texts.get(p, "") for p in instance_pages]
            
            # Build prompt from schema (NOT hardcoded)
            prompt = build_extraction_prompt(
                page_texts=texts,
                page_roles=template.page_roles,
                instance_index=idx,
                document_type=schema.document_type,
            )
            
            # Call LLM
            try:
                response = self.client.generate(
                    prompt=prompt,
                    system="You extract personal information from documents. Respond ONLY with valid JSON.",
                    use_case="template_extraction",
                    document_id=str(doc.id),
                )
                
                # Parse response into PIIRecord
                record = self._parse_extraction(response, doc, instance_pages)
                if record:
                    records.append(record)
                    
            except Exception as e:
                # LLM failed for this instance — log and continue
                # Pipeline will fall back to Presidio for this instance
                logger.warning(f"LLM extraction failed for instance {idx}: {e}")
                continue
        
        return records
    
    def _parse_extraction(self, response_text, doc, instance_pages) -> PIIRecord | None:
        """Parse LLM JSON response into a PIIRecord."""
        try:
            data = json.loads(response_text)
        except json.JSONDecodeError:
            return None
        
        rec = PIIRecord(
            record_id=str(uuid4()),
            source_document_id=str(doc.id),
            page_range=f"{instance_pages[0]+1}-{instance_pages[-1]+1}",
        )
        
        # Map extracted fields to PIIRecord.raw_* fields
        # This mapping uses entity type names, NOT document-specific field names
        FIELD_TO_RAW = {
            "PERSON": "raw_name",
            "LOCATION": "raw_address",
            "DATE_OF_BIRTH": "raw_dob",
            "EMAIL_ADDRESS": "raw_email",
            "PHONE_NUMBER": "raw_phone",
            "US_SSN": "raw_government_id",
            "NI_NUMBER": "raw_government_id",
            "AADHAAR": "raw_government_id",
            "US_DRIVER_LICENSE": "raw_government_id",
            "US_PASSPORT": "raw_government_id",
            "PAN_CARD": "raw_government_id",
        }
        
        GOV_ID_TYPES = {"US_SSN", "NI_NUMBER", "AADHAAR", "US_DRIVER_LICENSE", "US_PASSPORT", "PAN_CARD"}
        
        pii_types = []
        for entity_type, value in data.items():
            if value is None or value == "" or value == "null":
                continue
            
            raw_field = FIELD_TO_RAW.get(entity_type)
            if raw_field:
                setattr(rec, raw_field, str(value))
                pii_types.append(entity_type)
                
                # Track government ID type
                if entity_type in GOV_ID_TYPES:
                    rec.government_id_type = entity_type
        
        # Set entity type to PERSON if name was found
        if rec.raw_name:
            rec.entity_type = "PERSON"
        
        rec.pii_types_found = pii_types
        
        # Only return if at least a name was extracted
        return rec if rec.raw_name else None
    
    def validate_with_presidio(self, record: PIIRecord, engine) -> PIIRecord:
        """
        Optional: validate LLM-extracted values against Presidio patterns.
        If Presidio confirms the pattern matches, boost confidence.
        If Presidio rejects, flag for review.
        """
        # Validate government ID format
        if record.raw_government_id:
            results = engine.analyze(record.raw_government_id)
            if results:
                record.government_id_validated = True
            else:
                record.government_id_validated = False  # LLM extracted something Presidio doesn't recognize
        
        return record
```

#### 19c. Pipeline Integration

**File: `app/pipeline/two_phase.py`** — Update `extract_generator()`:

```python
# In extract_generator, replace the current extraction logic:

if schema and schema.template and schema.template.pages_per_instance > 1:
    # TEMPLATE MODE: LLM extracts structured fields directly
    if llm_enabled and ollama_client:
        extractor = LLMTemplateExtractor(ollama_client, db_session)
        pii_records = extractor.extract_all_instances(doc, schema, page_texts)
        
        if not pii_records:
            # LLM failed — fall back to Presidio + composite records
            pii_records = extract_with_template(doc, schema, detections_by_page)
    else:
        # No LLM — use Presidio + composite records (existing fallback)
        pii_records = extract_with_template(doc, schema, detections_by_page)
else:
    # NON-TEMPLATE: Presidio extracts per-page (existing flow)
    pii_records = [detection_to_pii_record(det, doc_id) for det in all_detections]

# Pass to EntityResolver — same either way
subjects = resolver.resolve(pii_records)
```

**Three extraction paths, automatic selection:**

| Document type | LLM available? | Extraction method |
|---|---|---|
| Template (repeating form) | Yes | LLM reads each instance → structured JSON → PIIRecord |
| Template (repeating form) | No | Presidio + composite records (Step 17 fallback) |
| Non-template (mixed/free-text) | Yes or No | Presidio per-page + schema filter (existing) |

#### 19d. Handling Large Documents Efficiently

A 450-page pension doc with 3 pages per template = 150 LLM calls. This needs optimization:

**Batching:** Send 3-5 instances per LLM call instead of 1:

```
"Extract information for 3 individuals from the following pages:

--- INDIVIDUAL 1 (pages 1-3) ---
[page texts]

--- INDIVIDUAL 2 (pages 4-6) ---
[page texts]

--- INDIVIDUAL 3 (pages 7-9) ---
[page texts]

Return a JSON ARRAY with one object per individual:
[
  {"PERSON": "...", "LOCATION": "...", "DATE_OF_BIRTH": "...", ...},
  {"PERSON": "...", "LOCATION": "...", "DATE_OF_BIRTH": "...", ...},
  {"PERSON": "...", "LOCATION": "...", "DATE_OF_BIRTH": "...", ...}
]
```

This reduces 150 LLM calls to ~50 (batches of 3). Configurable via `extraction_batch_size` in protocol config.

**Progress reporting:** Emit SSE progress events per batch:

```
{"stage": "extraction", "status": "progress", "message": "Extracted 15/150 individuals (10%)"}
```

**Memory management:** Process batches sequentially, don't load all 450 pages into memory. Use `_forget_page()` after each batch.

#### 19e. What this produces for the pension document

```
LLM reads pages 1-3 → extracts:
{
  "PERSON": "Mr K P Acheampong",
  "LOCATION": "85 Waltings Gardens, Shoot Up Hill, London NW2 3UD",
  "DATE_OF_BIRTH": "10-Aug-1959",
  "NI_NUMBER": "NE724362D"
}

LLM reads pages 4-6 → extracts:
{
  "PERSON": "Mr M S Alcock",
  "LOCATION": "3 Whitworth Road, Wellingborough, Northants NN8 1QQ",
  "DATE_OF_BIRTH": "29-Aug-1960",
  "NI_NUMBER": "WK393925C"
}

→ 2 PIIRecords with ALL fields populated
→ 2 NotificationSubjects
→ CSV:

individual_id | name              | address                                           | date_of_birth | government_id | gov_id_type | source_document      | source_pages
1             | Mr K P Acheampong | 85 Waltings Gardens, Shoot Up Hill, London NW2 3UD | 10-Aug-1959   | NE7****2D     | NI_NUMBER   | pension_transfer.pdf | 1-3
2             | Mr M S Alcock     | 3 Whitworth Road, Wellingborough, Northants NN8 1QQ| 29-Aug-1960   | WK3****5C     | NI_NUMBER   | pension_transfer.pdf | 4-6
```

#### 19f. Execution Prompt

```
@agent-general-purpose Read CLAUDE.md and docs/PLAN.md Step 19 for context.

Step 19: Schema-driven LLM extraction for template documents.

=== PROMPT GENERATOR ===

1. Create app/llm/extraction_prompts.py:
   - ENTITY_EXTRACTION_GUIDE dict: maps entity type names to human-readable
     extraction instructions (PERSON → "Full name including title",
     LOCATION → "Complete address", DATE_OF_BIRTH → "Date of birth in
     original format", NI_NUMBER → "UK National Insurance Number format",
     etc.). Cover all entity types in PROTOCOL_DEFAULT_ENTITIES.
   - build_extraction_prompt(page_texts, page_roles, instance_index,
     document_type) → str. Generates extraction prompt FROM the schema's
     pii_fields_expected. NOT hardcoded to any document type.

=== LLM TEMPLATE EXTRACTOR ===

2. Create app/structure/llm_template_extractor.py:
   - LLMTemplateExtractor class
   - extract_all_instances(doc, schema, page_texts) → list[PIIRecord]
     For each template instance: build prompt from schema → call LLM →
     parse JSON → create PIIRecord with raw_* fields populated.
   - _parse_extraction(response_text, doc, instance_pages) → PIIRecord
     Maps entity type keys in JSON to PIIRecord.raw_* fields.
     Sets government_id_type for gov ID detections.
     Returns None if no name extracted (skip instance).
   - Support batching: extract_batch_size parameter (default 3).
     Send multiple instances per LLM call, parse JSON array response.
   - Graceful fallback: if LLM fails for an instance, log warning,
     skip that instance (pipeline falls back to Presidio composite).

=== PIPELINE INTEGRATION ===

3. Update app/pipeline/two_phase.py extract_generator():
   Three-path extraction based on document type + LLM availability:
   
   Path A (template + LLM available):
     LLMTemplateExtractor.extract_all_instances()
     Fallback to Path B if LLM returns empty results
   
   Path B (template + no LLM):
     extract_with_template() — existing Presidio + composite records
   
   Path C (non-template):
     Existing per-detection record_mapper flow
   
   Selection logic:
   if schema.template and schema.template.pages_per_instance > 1:
       if llm_enabled:
           records = llm_extractor.extract_all_instances(...)
           if not records:
               records = extract_with_template(...)  # fallback
       else:
           records = extract_with_template(...)
   else:
       records = [detection_to_pii_record(det, doc_id) for det in detections]

4. Add extraction progress SSE events:
   Emit progress every batch: "Extracted 15/150 individuals (10%)"
   
5. Add extraction_batch_size to PROTOCOL_LLM_CONFIG defaults (default: 3).
   Also accept from protocol_configs.config_json (user override).

=== VALIDATION ===

6. Optional Presidio validation: after LLM extracts, run extracted values
   through Presidio to confirm patterns match. If Presidio confirms
   NI_NUMBER pattern on "NE724362D", boost confidence. If Presidio
   rejects, flag for manual review. Add validate_with_presidio() method.

=== TESTS ===

7. Create tests/test_llm_extraction.py:
   - build_extraction_prompt: generates from schema, not hardcoded
   - build_extraction_prompt: different schemas produce different prompts
   - build_extraction_prompt: pension schema includes NI_NUMBER, DOB
   - build_extraction_prompt: US medical schema includes US_SSN, not NI_NUMBER
   - _parse_extraction: JSON with all fields → PIIRecord fully populated
   - _parse_extraction: JSON with nulls → only non-null fields set
   - _parse_extraction: invalid JSON → returns None (graceful)
   - _parse_extraction: no PERSON in response → returns None
   - _parse_extraction: government_id_type set correctly (NI_NUMBER, US_SSN)
   - extract_all_instances: 6 pages, 3 per template → 2 PIIRecords
   - Batch mode: 3 instances per call → JSON array parsed correctly
   - Fallback: LLM fails → returns empty list (caller falls back to Presidio)
   - Pipeline integration: template + LLM → uses LLMTemplateExtractor
   - Pipeline integration: template + no LLM → uses extract_with_template
   - Pipeline integration: non-template → uses per-detection records
   - ENTITY_EXTRACTION_GUIDE covers all types in PROTOCOL_DEFAULT_ENTITIES

8. Run pytest on all changed files. Fix failures up to 3 attempts.
   Update CLAUDE.md.
```

---

### Step 20 — Vision-First Extraction Architecture (PENDING)

**Goal:** Replace the fragile text extraction pipeline (PyMuPDF → OCR → Presidio → LLM filter → schema filter) with a simpler vision-first approach: render PDF pages as images → send to vision-language model → get structured extraction directly. This eliminates the root cause of every major bug we've fought: text extraction losing document structure.

**Why vision-first for ALL documents, not just scanned:**

Every major extraction failure traces back to text extraction losing layout:

| Bug | Text extraction cause | Vision eliminates it |
|---|---|---|
| "Blunt NH828286D" concatenated | PyMuPDF flattens table rows | Model sees separate columns |
| NI number undetected | No Presidio recognizer | Model reads "National Insurance Number: NE724362D" |
| DOB not extracted | DD-MMM-YYYY not in Presidio | Model reads "Date of Birth: 10-Aug-1959" |
| Partial address ("London" only) | Multi-line address lost | Model sees full address block |
| "Lump Sum" as PERSON | spaCy misclassification | Model sees it in financial table |
| Dollar amounts as phone numbers | Flattened numbers look like phones | Model sees currency column |

Vision models see the actual page — labels next to values, table structure, section boundaries. Whether the PDF is born digital or scanned makes no difference to the model.

**Architecture simplification:**

```
BEFORE (complex, fragile — 6 processing layers):
  PDF → PyMuPDF text → OCR (scanned) → Presidio patterns → 
  LLM schema filter → LLM template extractor (text) → 
  defensive parsing → record mapper → entity resolver → CSV

AFTER (simple — 3 processing layers):
  PDF → render page image → vision model extracts fields →
  pattern validation → entity resolver → CSV
```

Presidio shifts from "primary extractor" to "pattern validator." All existing deny-lists, schema filters, and detection tuning become validation layers, not extraction layers.

---

#### 20a. Vision Extraction Client

**File: `app/llm/client.py`** — Extend OllamaClient:

```python
class OllamaClient:
    def __init__(self, ...):
        ...
        self.vision_model = settings.ollama_vision_model  # "qwen2.5vl:32b"
    
    def generate_with_images(self, prompt: str, images: list[str], *,
                              use_case: str, document_id: str = None,
                              model_override: str = None) -> str:
        """
        Send prompt + page images to a vision-language model.
        
        images: list of base64-encoded PNG images
        Uses the vision model (not the text model).
        Ollama API: POST /api/generate {"model": "qwen2.5vl:32b", "prompt": "...", "images": [...]}
        """
        model = model_override or self.vision_model
        
        if not self._settings.llm_assist_enabled:
            raise LLMDisabledError("LLM assist is disabled")
        
        payload = {
            "model": model,
            "prompt": prompt,
            "images": images,
            "stream": False,
        }
        
        start = time.monotonic()
        response = self._client.post(
            f"{self.base_url}/api/generate",
            json=payload,
            timeout=self.timeout_s * 2,  # vision calls take longer
        )
        self._last_latency_ms = int((time.monotonic() - start) * 1000)
        
        result = response.json()
        response_text = result.get("response", "")
        
        # Audit log
        if self.db_session:
            log_llm_call(
                self.db_session,
                document_id=document_id,
                use_case=use_case,
                model=model,
                prompt_text=f"[vision: {len(images)} images] {prompt[:500]}",
                response_text=response_text,
                latency_ms=self._last_latency_ms,
            )
        
        return response_text
    
    def is_vision_available(self) -> bool:
        """Check if the vision model is loaded and available."""
        try:
            resp = self._client.get(f"{self.base_url}/api/tags")
            models = [m["name"] for m in resp.json().get("models", [])]
            return any(self.vision_model.split(":")[0] in m for m in models)
        except Exception:
            return False
```

**New settings in `app/core/settings.py`:**

```python
ollama_vision_model: str = Field(default="qwen2.5vl:32b", alias="OLLAMA_VISION_MODEL")
use_vision_extraction: bool = Field(default=True, alias="USE_VISION_EXTRACTION")
vision_page_dpi: int = Field(default=150, alias="VISION_PAGE_DPI")
```

#### 20b. Page Image Renderer

**File: `app/pdf/renderer.py`** (new):

```python
import fitz  # PyMuPDF
import base64

def render_page_to_image(doc_path: str, page_number: int, dpi: int = 150) -> str:
    """
    Render a single PDF page as a base64-encoded PNG image.
    
    150 DPI is sufficient for text recognition while keeping image size
    manageable (~200-400KB per page).
    
    Memory-safe: opens doc, renders one page, closes immediately.
    """
    doc = fitz.open(doc_path)
    try:
        page = doc[page_number]
        mat = fitz.Matrix(dpi / 72, dpi / 72)  # scale factor
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")
        return base64.b64encode(img_bytes).decode("ascii")
    finally:
        doc.close()

def render_pages_to_images(doc_path: str, page_numbers: list[int], dpi: int = 150) -> list[str]:
    """
    Render multiple PDF pages as base64-encoded PNG images.
    Memory-safe: forgets each page after rendering.
    """
    doc = fitz.open(doc_path)
    images = []
    try:
        for page_num in page_numbers:
            if page_num >= doc.page_count:
                continue
            page = doc[page_num]
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat)
            img_bytes = pix.tobytes("png")
            images.append(base64.b64encode(img_bytes).decode("ascii"))
            doc._forget_page(page)  # release memory
        return images
    finally:
        doc.close()
```

#### 20c. Vision Document Extractor

**File: `app/structure/vision_extractor.py`** (new):

This replaces `llm_template_extractor.py` as the primary extraction path.

```python
class VisionDocumentExtractor:
    """
    Extracts PII from documents using vision-language models.
    
    Primary extraction path for ALL document types:
    - Template documents: extract from key pages of each instance
    - Non-template documents: extract from each page sequentially
    
    Falls back to text-based extraction if vision model unavailable.
    """
    
    def __init__(self, ollama_client, settings, db_session=None):
        self.client = ollama_client
        self.settings = settings
        self.db_session = db_session
    
    # --- Template documents ---
    
    def extract_template_instances(
        self,
        doc,                        # Document ORM object with file_path
        schema: DocumentSchema,     # with template populated
        instance_boundaries: list[list[int]],  # [[0,1,2], [3,4,5], ...]
    ) -> list[PIIRecord]:
        """
        Extract PII from all template instances using vision.
        
        For each instance, renders the KEY PAGE (member details page)
        as an image and sends to vision model. One image per individual,
        not all 3 pages — because the member details page has everything.
        
        Supports batching: sends multiple instance images in one call.
        """
        records = []
        batch_size = self.settings.get("extraction_batch_size", 3)
        
        # Identify which page within the template has the most PII
        key_page_offset = self._find_key_page_offset(schema.template)
        
        for batch_start in range(0, len(instance_boundaries), batch_size):
            batch = instance_boundaries[batch_start:batch_start + batch_size]
            
            # Render key page for each instance in batch
            batch_images = []
            batch_pages = []
            for instance_pages in batch:
                key_page = instance_pages[min(key_page_offset, len(instance_pages) - 1)]
                image = render_page_to_image(
                    doc.file_path, key_page, dpi=self.settings.vision_page_dpi
                )
                batch_images.append(image)
                batch_pages.append(instance_pages)
            
            # Build prompt
            prompt = self._build_batch_prompt(schema, len(batch_images))
            
            # Send to vision model
            try:
                response = self.client.generate_with_images(
                    prompt=prompt,
                    images=batch_images,
                    use_case="vision_template_extraction",
                    document_id=str(doc.id),
                )
                
                batch_records = self._parse_batch_response(response, doc, batch_pages)
                records.extend(batch_records)
                
            except Exception as e:
                logger.warning(f"Vision extraction failed for batch: {e}")
                continue
            
            # Progress reporting
            progress = min(batch_start + batch_size, len(instance_boundaries))
            yield_progress(f"Extracted {progress}/{len(instance_boundaries)} individuals")
        
        # Deduplicate within results
        records = self._deduplicate(records)
        return records
    
    # --- Non-template documents ---
    
    def extract_pages(
        self,
        doc,
        page_numbers: list[int],
        schema: DocumentSchema | None = None,
    ) -> list[PIIRecord]:
        """
        Extract PII from individual pages using vision.
        For non-template documents (letters, mixed content).
        
        Each page is rendered and sent to vision model.
        Results are aggregated into PIIRecords.
        """
        all_records = []
        batch_size = self.settings.get("extraction_batch_size", 5)
        
        for batch_start in range(0, len(page_numbers), batch_size):
            batch_page_nums = page_numbers[batch_start:batch_start + batch_size]
            images = render_pages_to_images(
                doc.file_path, batch_page_nums, dpi=self.settings.vision_page_dpi
            )
            
            prompt = self._build_page_extraction_prompt(schema, len(images))
            
            try:
                response = self.client.generate_with_images(
                    prompt=prompt,
                    images=images,
                    use_case="vision_page_extraction",
                    document_id=str(doc.id),
                )
                
                records = self._parse_page_response(response, doc, batch_page_nums)
                all_records.extend(records)
                
            except Exception as e:
                logger.warning(f"Vision page extraction failed: {e}")
                continue
        
        return self._deduplicate(all_records)
    
    # --- Prompt builders ---
    
    def _build_batch_prompt(self, schema, num_images):
        """Build prompt for batch template extraction."""
        fields = set()
        if schema.template:
            for role in schema.template.page_roles:
                fields.update(role.pii_fields_expected)
        fields.update(ALWAYS_EXTRACT_IF_PRESENT)
        
        field_guide = "\n".join(
            f"- {f}: {ENTITY_EXTRACTION_GUIDE.get(f, f'Extract {f}')}"
            for f in sorted(fields)
        )
        
        return f"""You are extracting personal information from {schema.document_type or 'a document'}.
Each image is one person's record page. There are {num_images} images = {num_images} individuals.

For EACH image, extract these fields:
{field_guide}

Return a JSON ARRAY with one object per image/individual:
[
  {{{', '.join(f'"{f}": "value or null"' for f in sorted(fields))}}},
  ...
]

RULES:
- Extract the EXACT value as it appears in the document
- For addresses, include the COMPLETE address (every line: street, area, city, county, postcode, country)
- For dates, preserve the original format (e.g., 10-Aug-1959)
- For names, include title if present (Mr, Mrs, Dr, Miss)
- For National Insurance Numbers, extract the full code (2 letters + 6 digits + 1 letter)
- If a field is not visible on the page, set it to null
- Do NOT guess or infer values not shown in the image
- Return ONLY valid JSON, no other text"""
    
    def _build_page_extraction_prompt(self, schema, num_images):
        """Build prompt for non-template page extraction."""
        return f"""You are extracting personal information from document pages.
There are {num_images} page images. Multiple individuals may appear on a single page.

For each individual found, extract:
- PERSON: full name with title
- LOCATION: complete address
- DATE_OF_BIRTH: date of birth
- NI_NUMBER: National Insurance Number (UK) or US_SSN (US) or equivalent
- EMAIL_ADDRESS: email
- PHONE_NUMBER: phone number

Return a JSON ARRAY with one object per individual found across all pages:
[
  {{"PERSON": "...", "LOCATION": "...", "DATE_OF_BIRTH": "...", ...}},
  ...
]

RULES:
- Same individual appearing on multiple pages = ONE entry (merge their data)
- Organization names are NOT individuals (skip companies, schemes, institutions)
- Financial terms (Lump Sum, Transfer Value, Pension) are NOT person names
- Return ONLY valid JSON"""
    
    def _find_key_page_offset(self, template):
        """Find which page in the template has the most PII fields."""
        if not template or not template.page_roles:
            return 1  # default: second page (member details)
        
        best_offset = 0
        best_count = 0
        for role in template.page_roles:
            count = len(role.pii_fields_expected)
            if count > best_count:
                best_count = count
                best_offset = role.page_offset
        return best_offset
    
    def _deduplicate(self, records):
        """Deduplicate records by normalized name."""
        seen = {}
        for rec in records:
            if not rec.raw_name:
                continue
            key = " ".join(rec.raw_name.lower().strip().split())
            if key not in seen:
                seen[key] = rec
            else:
                # Merge: fill missing fields from duplicate
                existing = seen[key]
                if not existing.raw_address and rec.raw_address:
                    existing.raw_address = rec.raw_address
                if not existing.raw_dob and rec.raw_dob:
                    existing.raw_dob = rec.raw_dob
                if not existing.raw_government_id and rec.raw_government_id:
                    existing.raw_government_id = rec.raw_government_id
                    existing.government_id_type = rec.government_id_type
                if not existing.raw_email and rec.raw_email:
                    existing.raw_email = rec.raw_email
                if not existing.raw_phone and rec.raw_phone:
                    existing.raw_phone = rec.raw_phone
        return list(seen.values())
```

#### 20d. Instance Boundary Detection (fixes the 51→149 gap)

Instead of relying on `pages_per_instance` (which the LLM got wrong as 8 instead of 3), scan for section markers:

**File: `app/pipeline/instance_detector.py`** (new):

```python
def find_instance_boundaries(
    doc_path: str,
    instance_marker: str | None = None,
    fallback_markers: list[str] = None,
) -> list[list[int]]:
    """
    Find where each template instance starts by scanning for repeating headers.
    
    Much more reliable than fixed pages_per_instance because it handles
    variable-length instances (some people have 3 pages, others 4+).
    
    Args:
        doc_path: path to PDF
        instance_marker: specific text to look for (from DocumentSchema.template)
        fallback_markers: generic markers to try if instance_marker not set
    
    Returns: [[0,1,2], [3,4,5], [6,7,8,9], ...] — pages per instance
    """
    import fitz
    
    if fallback_markers is None:
        fallback_markers = [
            "STATEMENT OF ENTITLEMENT",
            "SUMMARY OF DETAILS IN RESPECT OF",
            "MEMBER RECORD",
            "EMPLOYEE RECORD",
            "PATIENT RECORD",
            "CLAIMANT DETAILS",
            "INDIVIDUAL DETAILS",
            "PERSONAL DETAILS",
            "NOTIFICATION SUBJECT",
        ]
    
    markers = [instance_marker] if instance_marker else fallback_markers
    
    doc = fitz.open(doc_path)
    boundary_pages = []
    
    try:
        for page_num in range(doc.page_count):
            page = doc[page_num]
            text = page.get_text()[:500].upper()
            doc._forget_page(page)
            
            for marker in markers:
                if marker.upper() in text:
                    boundary_pages.append(page_num)
                    break
    finally:
        doc.close()
    
    if not boundary_pages:
        # No markers found — fall back to fixed page count
        return None  # caller should use get_instance_pages()
    
    # Convert boundary pages to instance page ranges
    instances = []
    for i, start in enumerate(boundary_pages):
        end = boundary_pages[i + 1] if i + 1 < len(boundary_pages) else doc.page_count
        instances.append(list(range(start, end)))
    
    return instances
```

#### 20e. Updated Pipeline Integration

**File: `app/pipeline/two_phase.py`** — Simplified extract_generator():

```python
# In extract_generator, replace the current 3-path extraction with:

# Step 1: Determine instance boundaries
if schema and schema.template:
    # Try marker-based detection first (most reliable)
    instance_marker = getattr(schema.template, 'instance_marker', None)
    instances = find_instance_boundaries(doc.file_path, instance_marker)
    
    if not instances:
        # Fall back to fixed page count
        instances = schema.template.get_instance_pages(doc.total_pages)
    
    is_template = True
else:
    instances = None
    is_template = False

# Step 2: Extract using vision (primary) or text (fallback)
vision_extractor = VisionDocumentExtractor(ollama_client, settings, db_session)

if vision_extractor.client.is_vision_available() and settings.use_vision_extraction:
    # VISION PATH (primary for all documents)
    if is_template and instances:
        pii_records = vision_extractor.extract_template_instances(doc, schema, instances)
    else:
        all_pages = list(range(doc.total_pages or 0))
        pii_records = vision_extractor.extract_pages(doc, all_pages, schema)
        
elif settings.llm_assist_enabled:
    # TEXT + LLM PATH (fallback when vision unavailable)
    if is_template and instances:
        text_extractor = LLMTemplateExtractor(ollama_client, db_session)
        pii_records = text_extractor.extract_all_instances(doc, schema, page_texts)
    else:
        pii_records = [detection_to_pii_record(det, doc_id) for det in all_detections]
        
else:
    # PRESIDIO-ONLY PATH (no LLM at all)
    pii_records = [detection_to_pii_record(det, doc_id) for det in all_detections]

# Step 3: Pattern validation on extracted records
pii_records = validate_extracted_records(pii_records)

# Step 4: Entity resolution (same for all paths)
subjects = resolver.resolve(pii_records)
```

**Three paths, clear priority:**

| Priority | Path | When used | Quality |
|---|---|---|---|
| 1 | Vision | Vision model available (default) | Highest |
| 2 | Text + LLM | Vision unavailable, LLM available | Medium |
| 3 | Presidio only | No LLM at all | Lowest |

#### 20f. Pattern Validation (Presidio's new role)

**File: `app/pii/pattern_validator.py`** (new):

```python
import re

# Validation patterns for extracted values
VALIDATION_PATTERNS = {
    "NI_NUMBER": re.compile(r'^[A-Z]{2}\d{6}[A-Z]$'),
    "US_SSN": re.compile(r'^\d{3}-\d{2}-\d{4}$'),
    "AADHAAR": re.compile(r'^\d{12}$'),
    "PAN_CARD": re.compile(r'^[A-Z]{5}\d{4}[A-Z]$'),
    "EMAIL_ADDRESS": re.compile(r'^[^@]+@[^@]+\.[^@]+$'),
    "UK_POSTCODE": re.compile(r'[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}', re.IGNORECASE),
}

DATE_PATTERNS = [
    re.compile(r'\d{1,2}-[A-Za-z]{3}-\d{4}'),    # 10-Aug-1959
    re.compile(r'\d{1,2}/\d{1,2}/\d{4}'),          # 10/08/1959
    re.compile(r'\d{4}-\d{2}-\d{2}'),               # 1959-08-10
    re.compile(r'\d{1,2}\s+[A-Za-z]+\s+\d{4}'),    # 10 August 1959
]

def validate_extracted_records(records: list[PIIRecord]) -> list[PIIRecord]:
    """
    Validate LLM-extracted values against known patterns.
    Sets validation flags but does NOT remove records.
    Invalid patterns are flagged for human review.
    """
    for rec in records:
        rec.validation_flags = []
        
        # Validate government ID
        if rec.raw_government_id and rec.government_id_type:
            pattern = VALIDATION_PATTERNS.get(rec.government_id_type)
            if pattern and not pattern.match(rec.raw_government_id.strip()):
                rec.validation_flags.append(f"gov_id_format_mismatch: {rec.government_id_type}")
        
        # Validate DOB is a real date
        if rec.raw_dob:
            if not any(p.search(rec.raw_dob) for p in DATE_PATTERNS):
                rec.validation_flags.append("dob_format_unrecognized")
        
        # Validate email
        if rec.raw_email:
            if not VALIDATION_PATTERNS["EMAIL_ADDRESS"].match(rec.raw_email):
                rec.validation_flags.append("email_format_invalid")
        
        # Validate address has some structure (not just a single word)
        if rec.raw_address:
            if len(rec.raw_address.split()) < 3:
                rec.validation_flags.append("address_too_short")
        
        # Validate name is not a financial term or organization
        if rec.raw_name:
            lower_name = rec.raw_name.lower().strip()
            if lower_name in FINANCIAL_TERM_DENY_LIST:
                rec.validation_flags.append("name_is_financial_term")
                rec.raw_name = None  # suppress
            elif is_likely_organization(rec.raw_name):
                rec.validation_flags.append("name_is_organization")
                rec.raw_name = None  # suppress
    
    # Remove records with no name (after validation suppressed it)
    return [r for r in records if r.raw_name]
```

#### 20g. Model Configuration per Protocol

**Update `app/core/constants.py`:**

```python
PROTOCOL_LLM_CONFIG = {
    "hipaa": {
        "llm_model": "qwen2.5:7b",
        "vision_model": "qwen2.5vl:32b",
        "extraction_batch_size": 3,
        "llm_pages_to_read": 5,
        "vision_page_dpi": 150,
    },
    "gdpr": {
        "llm_model": "gemma3:27b",
        "vision_model": "qwen2.5vl:32b",
        "extraction_batch_size": 3,
        "llm_pages_to_read": 3,
        "vision_page_dpi": 150,
    },
    "state_breach": {
        "llm_model": "qwen2.5:7b",
        "vision_model": "qwen2.5vl:32b",
        "extraction_batch_size": 5,
        "llm_pages_to_read": 3,
        "vision_page_dpi": 150,
    },
    # ... same pattern for all 8 protocols
}
```

User can override any setting in `protocol_configs.config_json`:

```json
{
    "vision_model": "llama3.2-vision:11b-instruct-fp16",
    "extraction_batch_size": 5,
    "vision_page_dpi": 200
}
```

#### 20h. Expected Results

**Pension document (CMG_Inc_0001352703.pdf, 453 pages, ~149 individuals):**

```
Instance detection: scans for "STATEMENT OF ENTITLEMENT" → finds 149 boundaries
Vision extraction: 149 instances / 3 per batch = 50 batches
Each batch: renders member details page → sends 3 images → gets 3 JSON objects
Time estimate: 50 batches × 10s = ~8 minutes on M4 Max with qwen2.5vl:32b

CSV output:
- 149 rows (one per individual)
- Each with: name, full address, DOB, NI number, source pages
- Zero "Lump Sum", zero single-word names, zero duplicates
- Pattern validation confirms NI number format, date format
```

**Washington CMD employment record (1 page):**

```
No template detected → single page extraction
Vision: sends 1 image → gets 1 JSON object
Time: ~10 seconds

CSV output:
- 1 row: Kristin B Aleshire, Hagerstown MD, SSN, phone
```

#### 20i. Execution Prompts (split into 2 runs)

**Step 20 Part 1 — Vision extraction core + instance detection:**

```
@agent-general-purpose Read CLAUDE.md and docs/PLAN.md Step 20 for context.

Step 20 Part 1: Vision-first extraction architecture.

=== VISION CLIENT ===

1. Add vision settings to app/core/settings.py:
   ollama_vision_model (default "qwen2.5vl:32b")
   use_vision_extraction (default True)
   vision_page_dpi (default 150)

2. Extend OllamaClient in app/llm/client.py:
   - Add vision_model property from settings
   - Add generate_with_images(prompt, images, use_case, document_id) method
     Sends to Ollama /api/generate with {"model": vision_model, "images": [...]}
     Timeout 2x normal (vision calls take longer)
     Audit logged to llm_call_logs
   - Add is_vision_available() method — checks /api/tags for vision model

=== PAGE RENDERER ===

3. Create app/pdf/renderer.py:
   - render_page_to_image(doc_path, page_number, dpi) → base64 PNG string
   - render_pages_to_images(doc_path, page_numbers, dpi) → list of base64 strings
   - Memory-safe: uses fitz._forget_page() after each render

=== INSTANCE BOUNDARY DETECTION ===

4. Create app/pipeline/instance_detector.py:
   - find_instance_boundaries(doc_path, instance_marker, fallback_markers) 
     → list[list[int]] or None
   - Scans all pages for repeating section headers
   - fallback_markers: ["STATEMENT OF ENTITLEMENT", "SUMMARY OF DETAILS IN RESPECT OF",
     "MEMBER RECORD", "EMPLOYEE RECORD", "PATIENT RECORD", "INDIVIDUAL DETAILS", etc.]
   - Returns page ranges per instance: [[0,1,2], [3,4,5], ...]
   - Returns None if no markers found (caller falls back to fixed pages_per_instance)

=== VISION EXTRACTOR ===

5. Create app/structure/vision_extractor.py:
   - VisionDocumentExtractor class
   - extract_template_instances(doc, schema, instance_boundaries) → list[PIIRecord]
     For each instance: render key page as image → send to vision model →
     parse JSON → create PIIRecord. Supports batching (3 instances per call).
   - extract_pages(doc, page_numbers, schema) → list[PIIRecord]
     For non-template docs: render pages → extract PII from each.
   - _build_batch_prompt(schema, num_images) — generates extraction prompt
     from schema and ENTITY_EXTRACTION_GUIDE. NOT hardcoded.
   - _build_page_extraction_prompt(schema, num_images) — for non-template pages
   - _find_key_page_offset(template) — finds page with most PII fields
   - _deduplicate(records) — merge records with same name
   - _parse_batch_response / _parse_page_response — JSON parsing with
     DEFENSIVE handling (same patterns as Step 19c fix)

=== PIPELINE INTEGRATION ===

6. Update app/pipeline/two_phase.py extract_generator():
   Three-path extraction with clear priority:
   
   Path 1 (Vision — primary):
     if vision available and use_vision_extraction:
       Detect instance boundaries (marker-based)
       If template: vision_extractor.extract_template_instances()
       If not template: vision_extractor.extract_pages()
   
   Path 2 (Text + LLM — fallback):
     if llm_assist_enabled:
       Existing LLMTemplateExtractor flow
   
   Path 3 (Presidio — last resort):
     Per-detection record creation
   
   ALL three paths produce list[PIIRecord] → same EntityResolver flow.
   
   CRITICAL: paths are EXCLUSIVE. Only ONE runs. No dual records.

=== TESTS ===

7. Create tests/test_vision_extraction.py:
   - render_page_to_image: returns base64 string
   - generate_with_images: sends correct payload with images array
   - is_vision_available: checks model list correctly
   - find_instance_boundaries: pension doc → 149 boundaries
   - find_instance_boundaries: no markers → returns None
   - extract_template_instances: 3 images → 3 PIIRecords with all fields
   - extract_pages: non-template → PIIRecords from each page
   - _deduplicate: same name twice → merged record
   - Pipeline: vision available → Path 1 taken, zero Presidio records
   - Pipeline: vision unavailable → Path 2 (text LLM) taken
   - Pipeline: no LLM → Path 3 (Presidio) taken

8. Run pytest on all changed files. Fix failures up to 3 attempts.
   Update CLAUDE.md.
```

**Step 20 Part 2 — Pattern validation + model config:**

```
@agent-general-purpose Read CLAUDE.md and docs/PLAN.md Step 20 for context.

Step 20 Part 2: Pattern validation and per-protocol model configuration.

=== PATTERN VALIDATOR ===

1. Create app/pii/pattern_validator.py:
   - VALIDATION_PATTERNS dict: regex patterns for NI_NUMBER, US_SSN,
     AADHAAR, PAN_CARD, EMAIL_ADDRESS, UK_POSTCODE
   - DATE_PATTERNS: list of date format regexes
   - validate_extracted_records(records) → validated records
     Checks each raw_* field against patterns. Sets validation_flags
     list on each record. Suppresses names that are financial terms
     or organizations (reuse FINANCIAL_TERM_DENY_LIST and
     is_likely_organization from context_deny_list.py).
     Removes records with no name after validation.

2. Wire validate_extracted_records() into extract_generator():
   After extraction (any path), before entity resolution:
   pii_records = validate_extracted_records(pii_records)

=== PER-PROTOCOL MODEL CONFIG ===

3. Update PROTOCOL_LLM_CONFIG in app/core/constants.py:
   Add vision_model and vision_page_dpi to each protocol config.
   See PLAN.md Step 20g for values.

4. Update protocol config resolution in extract_generator():
   When selecting vision_model, check:
   a) protocol_configs.config_json.vision_model (user override)
   b) PROTOCOL_LLM_CONFIG[protocol].vision_model (protocol default)
   c) settings.ollama_vision_model (global default)
   Pass resolved model to VisionDocumentExtractor.

5. Update .env.example and docker-compose.yml with new settings:
   OLLAMA_VISION_MODEL=qwen2.5vl:32b
   USE_VISION_EXTRACTION=true
   VISION_PAGE_DPI=150

=== TESTS ===

6. Create tests/test_pattern_validator.py:
   - Valid NI number: "NE724362D" → no flags
   - Invalid NI number: "INVALID" → flag set
   - Valid date: "10-Aug-1959" → no flags
   - Invalid date: "not a date" → flag set  
   - Financial term name: "Lump Sum" → suppressed (raw_name set to None)
   - Organization name: "William M Mercer Limited" → suppressed
   - Normal name: "Mr K P Acheampong" → no flags, kept
   - Address too short: "London" → flag set (but NOT suppressed)
   - Protocol model config: hipaa uses qwen2.5:7b, gdpr uses gemma3:27b
   - User override: config_json vision_model takes precedence

7. Run pytest on all changed files. Fix failures up to 3 attempts.
   Update CLAUDE.md.
```