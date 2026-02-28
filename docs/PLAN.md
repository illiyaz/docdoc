# Implementation Plan — Forentis AI (Cyber NotifAI Evolution)

Full phase-by-phase implementation details. See [CLAUDE.md](../CLAUDE.md) for project overview and conventions.

---

## Phase 1 — Deterministic Core (COMPLETE)

1. Storage policy + security foundation (`policies.py`, `security.py`)
2. ExtractedBlock + reader registry
3. PyMuPDF streaming reader + page classifier
4. PaddleOCR integration for scanned pages
5. Content onset detection + cross-page stitching
6. Excel reader (openpyxl, multi-tab)
7. Presidio + spaCy Layer 1 extraction — **with PHI/FERPA/SPI/PPRA patterns**
8. Checkpointing to PostgreSQL
9. Discovery task (filesystem + PostgreSQL connectors)
10. Basic FastAPI skeleton

**Phase 1 gate:** Every reader produces `ExtractedBlock` objects. PII is detected and hashed. No raw values in storage or logs. All 85+ patterns implemented. PASSED

---

## Phase 2 — Normalization + Rational Relationship Analysis (COMPLETE)

- Data normalization: phone → E.164, address → standard, name → canonical, email → lowercase
- `app/normalization/` package with one normalizer per field type
- `app/rra/entity_resolver.py` — links records to unique individuals via fuzzy name+address, exact email/phone, DOB matching
- `app/rra/deduplicator.py` — merges duplicate records with confidence score
- `app/db/models.py` additions: `NotificationSubject`
- RRA review queue: low-confidence merges surface for human review
- `app/rra/fuzzy.py` — soundex, Jaro-Winkler, names_match, addresses_match, government_ids_match, dobs_match

**Phase 2 gate:** Pipeline produces `NotificationSubject` list — one row per unique individual. Duplicates eliminated. Each subject has all PII types found, normalized contact info, source document provenance. PASSED

---

## Phase 3 — Protocol Configuration + Notification Delivery (COMPLETE)

- `app/protocols/` — Protocol dataclass, built-in protocols (HIPAA, GDPR, CCPA, state laws), YAML customization
- `app/protocols/loader.py` + `registry.py` — YAML loading, in-memory registry
- `app/notification/regulatory_threshold.py` — per-protocol rules: does this PII type trigger notification?
- `app/notification/list_builder.py` — applies protocol to NotificationSubjects, produces confirmed NotificationList
- `app/notification/email_sender.py` — SMTP delivery (local relay, no cloud dependency) with retry + rate limiting
- `app/notification/print_renderer.py` — print-ready PDF letters for postal delivery via WeasyPrint
- `app/notification/templates/` — HIPAA + default email and letter templates
- `app/db/models.py` additions: `NotificationList`
- 6 built-in protocol YAML files in `config/protocols/`

**Phase 3 gate:** End-to-end from breach dataset → email sent / letter printed. Fully automated path works without human review (for high-confidence runs). Protocol selection drives the entire downstream process. PASSED

---

## Phase 4 — Enhanced HITL + Comprehensive Audit Trail (COMPLETE)

- `app/audit/events.py` — 8 canonical event types, `VALID_EVENT_TYPES` frozenset
- `app/audit/audit_log.py` — `record_event()`, `get_subject_history()`, `get_events_by_type()` with full validation
- `app/review/roles.py` — `QUEUE_ROLE_MAP`, `required_role_for_queue()`, `can_action_queue()` (APPROVER override)
- `app/review/queue_manager.py` — `QueueManager` (create/assign/complete/get_queue), duplicate prevention, role validation
- `app/review/workflow.py` — `WorkflowEngine` state machine (AI_PENDING → HUMAN_REVIEW → LEGAL_REVIEW → APPROVED → NOTIFIED)
- `app/review/sampling.py` — `SamplingStrategy` (configurable rate, min/max, random sample, QC task creation)
- `app/db/models.py` — `AuditEvent` (Phase 4 schema, immutable), `ReviewTask` (Phase 4 schema, FK → notification_subjects)
- Append-only audit trail: every transition logged with actor, rationale, regulatory_basis
- Multi-role HITL: REVIEWER, LEGAL_REVIEWER, APPROVER, QC_SAMPLER
- Four review queues: low_confidence, escalation, qc_sampling, rra_review

**Phase 4 gate:** Every notification has a timestamped, role-attributed approval chain. Audit trail withstands regulatory scrutiny. QC sampling validates AI accuracy. PASSED

**Product is demo-ready. All pitch deck promises are backed by tested code.**

---

## Phase 5 — Forentis AI Evolution (IN PROGRESS)

Evolving Cyber NotifAI into **Forentis AI** — a full breach-analysis platform with Projects, editable Protocols, structured cataloging, density scoring, CSV export, configurable dedup, and LLM assist (Qwen 2.5 7B via Ollama, governance-gated). Deterministic pipeline remains primary; LLM is additive only.

**Implementation is split into 8 steps. All 8 steps are complete.**

### Step 1 — Schema + Migration (COMPLETE)

**5 new tables added to `app/db/models.py`:**

| Table | Primary key | Key columns | Server defaults | Relationships |
|---|---|---|---|---|
| `projects` | `id` (UUID) | `name` VARCHAR(512) NOT NULL, `description` TEXT, `status` VARCHAR(32), `created_by` VARCHAR(128), `created_at`, `updated_at` | `status='active'` | 1:N → protocol_configs, ingestion_runs, density_summaries, export_jobs |
| `protocol_configs` | `id` (UUID) | `project_id` UUID FK→projects.id (CASCADE), `base_protocol_id` VARCHAR(128), `name` VARCHAR(256) NOT NULL, `config_json` JSON NOT NULL, `status` VARCHAR(32), `created_at`, `updated_at` | `status='draft'` | N:1 → project |
| `density_summaries` | `id` (UUID) | `project_id` UUID FK→projects.id (CASCADE), `document_id` UUID FK→documents.id (CASCADE, nullable — NULL=project-level), `total_entities` INT NOT NULL, `by_category` JSON, `by_type` JSON, `confidence` VARCHAR(16), `confidence_notes` TEXT, `created_at` | — | N:1 → project |
| `export_jobs` | `id` (UUID) | `project_id` UUID FK→projects.id (CASCADE), `protocol_config_id` UUID FK→protocol_configs.id (SET NULL, nullable), `export_type` VARCHAR(32), `status` VARCHAR(32), `file_path` VARCHAR(2048), `row_count` INT, `filters_json` JSON, `created_at`, `completed_at` | `status='pending'` | N:1 → project |
| `llm_call_logs` | `id` (UUID) | `document_id` UUID FK→documents.id (SET NULL, nullable), `use_case` VARCHAR(64) NOT NULL, `model` VARCHAR(128) NOT NULL, `prompt_text` TEXT NOT NULL, `response_text` TEXT NOT NULL, `decision` VARCHAR(128), `accepted` BOOLEAN, `latency_ms` INT, `token_count` INT, `created_at` | — | — |

**3 existing tables extended:**

| Table | New columns | Design decisions |
|---|---|---|
| `ingestion_runs` | `project_id` UUID FK→projects.id (SET NULL, **nullable** for backward compat) | Existing jobs without a project keep working |
| `documents` | `structure_class` VARCHAR(32) nullable, `can_auto_process` BOOLEAN NOT NULL default true, `manual_review_reason` VARCHAR(256) nullable | Cataloger (Step 3) will populate these |
| `notification_subjects` | `project_id` UUID FK→projects.id (SET NULL, **nullable**) | Same backward-compat pattern |
| `notification_lists` | `project_id` UUID FK→projects.id (SET NULL, **nullable**) | Same backward-compat pattern |

**New settings in `app/core/settings.py`:**

```python
llm_assist_enabled: bool = Field(default=False, alias="LLM_ASSIST_ENABLED")
ollama_url: str = Field(default="http://localhost:11434", alias="OLLAMA_URL")
ollama_model: str = Field(default="qwen2.5:7b", alias="OLLAMA_MODEL")
ollama_timeout_s: int = Field(default=60, alias="OLLAMA_TIMEOUT_S")
```

**Migration:** `alembic/versions/0005_projects_and_protocols.py` (revises `0004_add_audit_and_review`)
- Creates 5 new tables
- `op.add_column` + `op.create_foreign_key` for the 4 extended tables
- Full `downgrade()` reverses all changes

**Schema tests:** `tests/test_schema.py` — 12 tests (5 new: `test_project_columns_exist`, `test_protocol_config_columns_exist`, `test_density_summary_columns_exist`, `test_export_job_columns_exist`, `test_llm_call_log_columns_exist`). Existing tests updated to assert new columns on `ingestion_runs` (`project_id`), `documents` (`structure_class`, `can_auto_process`, `manual_review_reason`), `notification_subjects` (`project_id`), `notification_lists` (`project_id`), and new server defaults (`projects.status='active'`, `protocol_configs.status='draft'`, `export_jobs.status='pending'`, `documents.can_auto_process=true`).

**Total DB tables: 17** (12 original + 5 new)

### Step 2 — Project + Protocol API (COMPLETE)

**New route files:**

| File | Prefix | Endpoints |
|---|---|---|
| `app/api/routes/projects.py` | `/projects` | `POST /projects`, `GET /projects`, `GET /projects/{id}`, `PATCH /projects/{id}`, `GET /projects/{id}/catalog-summary`, `GET /projects/{id}/density` |
| `app/api/routes/protocols.py` | `/projects/{project_id}/protocols` | `POST .../protocols`, `GET .../protocols`, `GET .../protocols/{pid}`, `PATCH .../protocols/{pid}` |

**Route details:**

| Method + Path | Request body | Response | Notes |
|---|---|---|---|
| `POST /projects` | `{name, description?, created_by?}` | Project dict with `id`, `status='active'` | Creates project |
| `GET /projects` | — | `[Project, ...]` ordered by created_at desc | List all |
| `GET /projects/{id}` | — | Project + `protocols: [ProtocolConfig, ...]` | Detail with nested protocols |
| `PATCH /projects/{id}` | `{name?, description?, status?}` | Updated project | Status must be `active\|archived\|completed` |
| `GET /projects/{id}/catalog-summary` | — | `{total_documents, auto_processable, manual_review, by_file_type, by_structure_class}` | Aggregates from documents via ingestion_runs.project_id |
| `GET /projects/{id}/density` | — | `{project_summary, document_summaries: [...]}` | Reads from density_summaries table |
| `POST .../protocols` | `{name, base_protocol_id?, config_json}` | ProtocolConfig dict with `status='draft'` | Creates editable protocol config |
| `GET .../protocols` | — | `[ProtocolConfig, ...]` | List for project |
| `GET .../protocols/{pid}` | — | ProtocolConfig dict | Detail |
| `PATCH .../protocols/{pid}` | `{name?, config_json?, status?}` | Updated ProtocolConfig | Returns 409 if `status='locked'`; status must be `draft\|active\|locked` |

**`config_json` blob structure** (stored in `protocol_configs.config_json`):
```json
{
  "target_entity_types": ["US_SSN", "EMAIL_ADDRESS"],
  "storage_policy": "strict",
  "sampling_rate": 0.10,
  "sampling_min": 5,
  "sampling_max": 100,
  "confidence_threshold": 0.75,
  "dedup_anchors": ["ssn", "email", "phone"],
  "export_fields": ["canonical_name", "canonical_email"],
  "reviewers": {"investigator": [], "qa": [], "compliance": []}
}
```

**Wired into `app/api/main.py`:**
```python
from app.api.routes.projects import router as projects_router
from app.api.routes.protocols import router as protocols_router
app.include_router(projects_router)
app.include_router(protocols_router)
```

**`CreateJobBody` extended** in `app/api/routes/jobs.py`:
```python
class CreateJobBody(BaseModel):
    project_id: str | None = None          # links job to project
    protocol_config_id: str | None = None  # per-engagement protocol
```

**Design decisions:**
- All new project_id FKs are **nullable** — existing jobs/subjects/lists created before projects were introduced continue working without modification
- `ProtocolConfig.status` lifecycle: `draft` → `active` → `locked`. Locked configs cannot be edited (409 returned). This prevents mid-job config changes.
- `config_json` is a single JSON column rather than many typed columns — avoids schema churn as new config fields are added
- Catalog summary and density endpoints are on the projects router (not separate) because they are project-scoped views

**API tests:** `tests/test_api.py` — 13 new tests in `TestProjectsCRUD` (7 tests) and `TestProtocolConfigCRUD` (6 tests). Covers create, list, get, update, not-found, invalid status, locked-edit-409.

**All 1057 tests passing after Steps 1–2.**

### Step 3 — Cataloger Task (File Structure Classifier) (COMPLETE)

**New file: `app/tasks/cataloger.py`**

Pure-function file extension classifier + ORM task that runs AFTER discovery, BEFORE extraction.

| Component | Details |
|---|---|
| `classify_extension(ext)` | Pure function: extension string → `StructureClass` literal. Case-insensitive, whitespace-stripped. |
| `CatalogerTask(db_session)` | ORM task: takes list of `Document` objects, sets `structure_class`, `can_auto_process`, `manual_review_reason`, flushes once. |
| `StructureClass` | `Literal["structured", "semi-structured", "unstructured", "non-extractable"]` |

**Extension classification map:**

| Structure class | Extensions |
|---|---|
| `structured` | csv, xlsx, xls, parquet, avro |
| `semi-structured` | html, htm, xml, eml, msg |
| `unstructured` | pdf, docx |
| `non-extractable` | everything else (dat, txt, zip, exe, jpg, unknown, empty) |

**Catalog field logic:**
- Known extensions → `can_auto_process=True`, `manual_review_reason=None`
- Non-extractable with extension → `can_auto_process=False`, reason = `"File type '.{ext}' is not supported for automated extraction"`
- Non-extractable without extension → `can_auto_process=False`, reason = `"File has no recognized extension"`

**Exported constants:** `_STRUCTURED_EXTENSIONS`, `_SEMI_STRUCTURED_EXTENSIONS`, `_UNSTRUCTURED_EXTENSIONS`, `_ALL_KNOWN_EXTENSIONS` (frozensets)

**Tests:** `tests/test_cataloger.py` — 3 test classes:
- `TestClassifyExtension` (8 tests + parametrized): all extensions, empty string, case insensitivity, whitespace stripping, union correctness
- `TestCatalogerTask` (19 tests): every file type, mixed batch, empty list, DB persistence, non-extractable persistence
- `TestCatalogSummaryIntegration` (1 test): end-to-end project → ingestion run → documents → cataloger → API endpoint

### Step 4 — Density Scoring Task (COMPLETE)

**New file: `app/tasks/density.py`**

Pure-function category mapping + confidence aggregation + ORM task that runs AFTER extraction completes.

| Component | Details |
|---|---|
| `classify_entity_type(entity_type)` | Pure function: entity type string → category (`"PHI"`, `"PFI"`, or `"PII"`). Case-insensitive keyword matching. |
| `compute_confidence(scores)` | Pure function: list of confidence scores → `ConfidenceResult(label, notes)`. Thresholds: high (>80% >= 0.75), low (>30% < 0.50), partial (otherwise). |
| `_compute_density(extractions)` | Pure function: list of `ExtractionInput` → `(total_entities, by_category, by_type, confidence_result)` |
| `DensityTask(db_session)` | ORM task: takes project_id + optional extraction inputs, creates per-document + project-level `DensitySummary` rows. |
| `ExtractionInput` | Lightweight dataclass: `document_id`, `pii_type`, `confidence_score`. Decouples pure logic from ORM. |

**Entity type → category mapping:**

| Category | Keyword triggers (in entity type string) |
|---|---|
| PHI | MEDICAL, HEALTH, MEDICARE, MRN, NPI, DEA, ICD, HICN |
| PFI | CREDIT_CARD, BANK, FINANCIAL, ROUTING, IBAN, SWIFT |
| PII | Everything else (SSN, EMAIL, PHONE, NAME, ADDRESS, DOB, PASSPORT, etc.) |

**Confidence aggregation logic:**
- `"high"` — >80% of valid scores >= 0.75
- `"low"` — >30% of valid scores < 0.50 (includes "OCR quality issues" note)
- `"partial"` — otherwise
- None scores tracked separately in notes but excluded from threshold calculations

**DensityTask.run() behavior:**
- Accepts optional `extraction_inputs` list or queries DB via `ingestion_runs.project_id → documents → extractions`
- Creates one `DensitySummary` row per document (grouped by `document_id`)
- Creates one project-level `DensitySummary` row with `document_id=NULL`
- `confidence_notes` stored as JSON-encoded list of strings
- Flushes once at end

**Tests:** `tests/test_density.py` — 4 test classes:
- `TestClassifyEntityType` (5 tests + parametrized): PHI/PFI/PII mapping, case insensitivity
- `TestComputeConfidence` (11 tests): high/partial/low thresholds, boundary cases, empty/None scores
- `TestComputeDensity` (4 tests): basic density, empty, duplicates, all-PHI
- `TestDensityTask` (12 tests): per-document + project summaries, category/type persistence, confidence persistence
- `TestDensityEndpointIntegration` (3 tests): endpoint returns summaries, empty project, 404

**All 1171 tests passing after Steps 1–4.**

### Step 5 — Configurable Dedup Anchors (COMPLETE)

**Modified file: `app/rra/entity_resolver.py`**

Makes the RRA entity resolver's matching signals configurable via an `active_anchors` parameter. When a project's protocol config specifies `dedup_anchors` (e.g., `["ssn", "email", "phone"]`), only those signals are evaluated during entity resolution. When no anchors are specified (or `None`), all signals are used (backward compatible).

| Component | Details |
|---|---|
| `VALID_ANCHORS` | `frozenset({"ssn", "email", "phone", "name_dob", "name_address", "name"})` — exported constant |
| `ALL_ANCHORS` | Alias for `VALID_ANCHORS` — convenience constant |
| `_resolve_anchors(active_anchors)` | Pure function: normalizes `None`/empty/list/frozenset to validated frozenset. Raises `ValueError` for invalid anchor names. Case-insensitive, whitespace-stripped. |
| `build_confidence(r1, r2, *, active_anchors=None)` | Extended with keyword-only `active_anchors` param. Each signal block is gated by anchor membership check. |
| `EntityResolver.resolve(records, *, active_anchors=None)` | Extended with keyword-only `active_anchors` param. Validates once via `_resolve_anchors`, passes resolved frozenset to all `build_confidence` calls. |

**Anchor → signal mapping:**

| Anchor name | Signal | Confidence boost |
|---|---|---|
| `ssn` | Government ID match (SSN, passport, driver license, etc.) | +0.50 |
| `email` | Exact email match (normalized) | +0.40 |
| `phone` | Exact phone match | +0.35 |
| `name_dob` | Name match + DOB match | +0.35 |
| `name_address` | Name match + address match (fuzzy) | +0.25 |
| `name` | Name match alone | +0.10 |

**Design decisions:**
- `name_dob` and `name_address` anchors control their respective compound signals but do NOT automatically include the `name`-alone (+0.10) bonus. The `name` anchor must be explicitly included if the +0.10 bonus is desired. This gives fine-grained control.
- Empty list `[]` defaults to all anchors (same as `None`) for backward compatibility.
- Invalid anchor names raise `ValueError` immediately (fail-fast), caught at both `build_confidence` and `resolve` entry points.
- Anchor validation happens once per `resolve` call, not per-pair, for performance.

**Integration with protocol_configs:** The `config_json` blob in `protocol_configs` already has a `dedup_anchors` field (defined in Step 2). Callers can extract `config_json["dedup_anchors"]` and pass it to `EntityResolver.resolve(records, active_anchors=dedup_anchors)`.

**Tests:** `tests/test_entity_resolver.py` — 3 new test classes (34 new tests):
- `TestResolveAnchors` (10 tests): None returns all, empty list returns all, single anchor, multiple anchors, case insensitivity, whitespace stripping, invalid raises, frozenset input, all valid, ALL_ANCHORS constant
- `TestBuildConfidenceWithAnchors` (14 tests): default None, ssn-only, email-only, phone-only, name_dob without name, name_dob+name, name_address without name, name-only, ssn+email stacking, disabled ssn/email/phone, invalid raises, name signals excluded by non-name anchors
- `TestResolveWithAnchors` (10 tests): backward compat, ssn merges, email prevents ssn merge, phone merges, mixed selective merge, all anchors same as default, invalid raises, empty list uses all, name_dob review flag, review flag with anchors

**All 1205 tests passing after Steps 1–5.**

### Step 6 — CSV Export (COMPLETE)

**New files:**

| File | Purpose |
|---|---|
| `app/export/__init__.py` | Package init |
| `app/export/csv_exporter.py` | CSV export logic — pure functions + ORM-integrated exporter |
| `app/api/routes/exports.py` | FastAPI routes: create, list, get, download exports |

**`app/export/csv_exporter.py` components:**

| Component | Details |
|---|---|
| `DEFAULT_EXPORT_FIELDS` | `["canonical_name", "canonical_email", "canonical_phone", "pii_types_found", "merge_confidence", "review_status"]` |
| `ALLOWED_EXPORT_FIELDS` | Frozenset of 10 safe-to-export fields (no raw PII fields). Unknown fields silently dropped. |
| `_mask_email(email)` | Pure function: any email → `"***@***.***"`. None/empty → `""`. |
| `_mask_phone(phone)` | Pure function: any phone → `"***-***-{last4}"`. None/empty → `""`. |
| `_mask_address(addr)` | Pure function: dict → state + zip only. Street/city removed. None → `""`, empty dict → `"***"`. |
| `_format_value(field, value)` | Pure function: applies field-specific masking, JSON-serializes lists/dicts, formats floats to 4 decimal places. |
| `resolve_export_fields(protocol_config)` | Reads `export_fields` from `config_json`, validates against `ALLOWED_EXPORT_FIELDS`, falls back to `DEFAULT_EXPORT_FIELDS`. |
| `SubjectRow` | Lightweight dataclass projection of `NotificationSubject`. `from_orm()` class method. `get(field)` accessor. |
| `build_csv_content(rows, fields)` | Pure function: builds CSV string with header + masked data rows. No DB or IO. |
| `CSVExporter(db_session)` | ORM task: queries subjects, applies filters, writes CSV file, creates/updates `ExportJob` record. |

**CSVExporter.run() behavior:**
- Creates `ExportJob` record with `status='pending'`
- Resolves export fields from optional `ProtocolConfig`
- Queries `NotificationSubject` rows filtered by `project_id`
- Applies optional filters: `confidence_threshold` (SQL), `review_status` (SQL), `entity_types` (Python post-filter for SQLite compat)
- Builds masked CSV content via `build_csv_content()`
- Writes to `{output_dir}/export_{job_id}.csv`
- Updates job to `status='completed'` with `file_path`, `row_count`, `completed_at`
- On exception: sets `status='failed'`, re-raises

**PII safety (critical):**
- `canonical_email` always masked to `***@***.***`
- `canonical_phone` always masked to `***-***-{last4}`
- `canonical_address` stripped to state + zip only (street/city removed)
- `canonical_name` passes through (not considered raw PII — it is the normalized display name)
- No raw PII fields (`raw_value`, `raw_value_encrypted`, `hashed_value`) are in `ALLOWED_EXPORT_FIELDS`

**API routes (`app/api/routes/exports.py`):**

| Method + Path | Request body | Response | Notes |
|---|---|---|---|
| `POST /projects/{id}/exports` | `{protocol_config_id?, filters?}` | ExportJob dict with `status='completed'` | Triggers synchronous CSV export |
| `GET /projects/{id}/exports` | — | `[ExportJob, ...]` ordered by created_at desc | List all exports for project |
| `GET /projects/{id}/exports/{eid}` | — | ExportJob dict | Detail |
| `GET /projects/{id}/exports/{eid}/download` | — | FileResponse (text/csv) | Returns 400 if not completed, 404 if file missing |

**Wired into `app/api/main.py`:**
```python
from app.api.routes.exports import router as exports_router
app.include_router(exports_router)  # after protocols_router, before jobs_router
```

**Tests:** `tests/test_export.py` — 8 test classes (61 tests):
- `TestMaskEmail` (3 tests): real email, None, empty
- `TestMaskPhone` (5 tests): E.164, formatted, None, empty, short
- `TestMaskAddress` (4 tests): full address, None, empty dict, state-only
- `TestFormatValue` (9 tests): None, email/phone/address masking, list→JSON, float formatting, bool, string, UUID
- `TestResolveExportFields` (6 tests): default, from config, unknown dropped, all unknown fallback, empty list fallback, no key fallback
- `TestBuildCSVContent` (6 tests): header row, data rows with masking, no raw email/phone, empty, multiple rows
- `TestSubjectRow` (2 tests): from_orm, get accessor
- `TestCSVExporter` (12 tests): creates job, file written, no raw email/phone/address, multiple subjects, empty project, protocol config fields, confidence/status/entity_type filters, filters stored, job persisted
- `TestExportAPI` (13 tests): create, create 404, list, list empty, list 404, get, get 404, download, download no raw email, download 404, with protocol config, with filters, response shape

**All 1266 tests passing after Steps 1–6.**

### Step 7 — LLM Integration (Qwen 2.5 7B via Ollama) (COMPLETE)

**New files:**

| File | Purpose |
|---|---|
| `app/llm/__init__.py` | Package init |
| `app/llm/client.py` | Governance-gated Ollama client wrapper |
| `app/llm/prompts.py` | Prompt templates for LLM-assisted classification |
| `app/llm/audit.py` | LLM call auditing (log + query) |

**`app/llm/client.py` — OllamaClient:**

| Component | Details |
|---|---|
| `OllamaClient` | Synchronous client wrapping Ollama's `POST /api/generate` endpoint via `httpx` |
| `generate(prompt, system, *, use_case, document_id)` | Sends prompt to Ollama, returns response text. Governance-gated: raises `LLMDisabledError` when `llm_assist_enabled=False`. Logs all calls to `llm_call_logs` table when `db_session` provided. Measures wall-clock latency. |
| `is_available()` | Health check via `GET /api/tags`. Returns `False` if Ollama not running (never raises). |
| `last_latency_ms` | Property: latency of most recent `generate()` call in milliseconds. |
| `LLMDisabledError` | Raised when `llm_assist_enabled=False` (governance gate). |
| `LLMConnectionError` | Raised when Ollama is unreachable (`httpx.ConnectError`, `httpx.HTTPError`). |
| `LLMTimeoutError` | Raised when request exceeds `ollama_timeout_s`. |

**Constructor parameters:** `base_url` (default: `settings.ollama_url`), `model` (default: `settings.ollama_model`), `timeout_s` (default: `settings.ollama_timeout_s`), `db_session` (optional, enables audit logging).

**PII safety:** Lightweight regex scan warns if prompt contains patterns matching SSN (XXX-XX-XXXX), 9-digit numbers, or 16-digit credit card numbers. Warning only (does not block) -- callers are responsible for masking PII before passing to the client.

**`app/llm/prompts.py` — Prompt templates:**

| Template | Use case | Format placeholders |
|---|---|---|
| `CLASSIFY_AMBIGUOUS_ENTITY` | Classify an entity with low deterministic confidence | `context_window`, `masked_value`, `detection_method`, `candidate_type`, `confidence_score` |
| `ASSESS_EXTRACTION_CONFIDENCE` | Assess if a low-confidence extraction is a true positive | `entity_type`, `masked_value`, `extraction_layer`, `pattern_name`, `original_confidence`, `context_window` |
| `SUGGEST_ENTITY_CATEGORY` | Suggest applicable data categories for an entity type | `entity_type`, `entity_description`, `current_categories` |
| `SYSTEM_PROMPT` | Shared system prompt for all LLM calls | (none) |

All templates instruct the LLM to respond ONLY with valid JSON. `PROMPT_TEMPLATES` dict provides programmatic access by name.

**`app/llm/audit.py` — LLM call auditing:**

| Function | Details |
|---|---|
| `log_llm_call(db_session, *, document_id, use_case, model, prompt_text, response_text, decision, accepted, latency_ms, token_count)` | Creates `LLMCallLog` row. All params map to table columns. `document_id` optional. PII safety check on `prompt_text` (warns if potential raw PII detected). |
| `get_llm_calls(db_session, *, document_id, use_case, limit)` | Queries `llm_call_logs` with optional filters. Returns list of dicts. Default limit 100. Ordered by `created_at` descending. |

**Design decisions:**
- LLM is ADDITIVE ONLY -- the deterministic pipeline works without it. `llm_assist_enabled=False` by default.
- LLM output is NEVER used directly -- validated against Layer 1/2 patterns before acceptance.
- Every LLM call is logged with full input/output for audit trail (`llm_call_logs` table from migration 0005).
- Ollama runs locally (air-gap safe). No cloud API calls.
- `httpx` used for synchronous HTTP calls (matches pipeline execution model).
- `db_session` is optional on client constructor -- health checks and availability probes do not require DB.

**Tests:** `tests/test_llm.py` — 10 test classes (55 tests):
- `TestOllamaClientDisabled` (2 tests): governance gate raises `LLMDisabledError`
- `TestOllamaClientIsAvailable` (4 tests): health check with mocked httpx (connect error, success, 500, timeout)
- `TestOllamaClientGenerate` (4 tests): successful generation, system prompt, model, stream=false
- `TestOllamaClientTimeout` (2 tests): timeout → `LLMTimeoutError`, latency still tracked
- `TestOllamaClientConnectionError` (2 tests): connect error → `LLMConnectionError`, HTTP error handling
- `TestOllamaClientLatency` (3 tests): None before first call, set after success, updated on second call
- `TestOllamaClientAuditLogging` (4 tests): logged to DB, no logging without session, document_id logged, prompt text logged
- `TestOllamaClientPIISafety` (4 tests): SSN detection, clean text, credit card, CC with spaces
- `TestPromptTemplates` (11 tests): formatting, JSON instruction, ONLY keyword, system prompt, registry, response keys, valid strings
- `TestLogLLMCall` (5 tests): creates record, all fields, nullable defaults, queryable, multiple records
- `TestGetLLMCalls` (8 tests): empty, all records, filter by use_case, filter by document_id, limit, default limit, combined filters, result dict shape, null document_id
- `TestAuditPIISafety` (5 tests): SSN pattern, masked text, credit card, clean text, warns on PII

**All 1400 tests passing after Steps 1–7.**

### Step 8 — Frontend + Rename to Forentis AI (COMPLETE)

**New files:**

| File | Purpose |
|---|---|
| `frontend/src/pages/Projects.tsx` | Projects list page with create form, status badges, project cards |
| `frontend/src/pages/ProjectDetail.tsx` | Project detail page with 5 tabs: Overview, Protocols, Catalog, Density, Exports |

**Modified files:**

| File | Changes |
|---|---|
| `frontend/src/App.tsx` | Added `/projects` and `/projects/:id` routes, added Projects nav item, renamed sidebar brand from "Cyber NotifAI" to "Forentis AI" |
| `frontend/src/api/client.ts` | Added 15 TypeScript interfaces and 12 API functions for projects, protocol configs, catalog summary, density, and exports |
| `frontend/src/pages/Dashboard.tsx` | Renamed header from "Cyber NotifAI" to "Forentis AI" |
| `frontend/index.html` | Updated `<title>` to "Forentis AI" |
| `frontend/package.json` | Renamed package from "frontend" to "forentis-ai" |
| `frontend/vite.config.ts` | Added `/projects` proxy rule for API forwarding |
| `app/core/settings.py` | Changed default `app_name` from "DocDoc API" to "Forentis AI" |
| `.env.example` | Updated `APP_NAME` to "Forentis AI" |
| `docker-compose.yml` | Updated `APP_NAME` to "Forentis AI" |
| `PORTS.md` | Renamed all "Cyber NotifAI" references to "Forentis AI" |
| `CLAUDE.md` | Renamed header and product goal to "Forentis AI", marked Step 8 COMPLETE |

**Projects page (`Projects.tsx`):**
- Lists all projects fetched from `GET /projects` with status badges, descriptions, and timestamps
- Create Project form with name, description, and created_by fields
- Grid layout with clickable project cards that navigate to detail page
- Empty state with icon when no projects exist
- Error and loading states handled

**ProjectDetail page (`ProjectDetail.tsx`):**
- 5-tab interface: Overview, Protocols, Catalog, Density, Exports
- **Overview tab:** Displays project info (name, status, description, created_by, created_at). Inline edit mode with save/cancel.
- **Protocols tab:** Lists protocol configurations with expandable JSON config view. Create protocol config form with name, base_protocol_id, and JSON config. Status badges (draft/active/locked).
- **Catalog tab:** Fetches catalog summary from `GET /projects/{id}/catalog-summary`. Shows total documents, auto-processable vs manual review counts, breakdowns by file type and structure class.
- **Density tab:** Fetches density from `GET /projects/{id}/density`. Shows project-level summary (total entities, confidence, by category, by type). Per-document density table.
- **Exports tab:** Lists export jobs from `GET /projects/{id}/exports`. Create export button. Download links for completed exports. Status badges (pending/completed/failed).
- Breadcrumb navigation (Projects > Project Name)

**API client additions (`client.ts`):**
- Interfaces: `ProjectSummary`, `ProjectDetail`, `CreateProjectBody`, `UpdateProjectBody`, `ProtocolConfigSummary`, `CreateProtocolConfigBody`, `UpdateProtocolConfigBody`, `CatalogSummary`, `DensitySummaryItem`, `DensityResponse`, `ExportJobSummary`, `CreateExportBody`
- Functions: `createProject`, `listProjects`, `getProject`, `updateProject`, `getCatalogSummary`, `getDensity`, `createProtocolConfig`, `listProtocolConfigs`, `getProtocolConfig`, `updateProtocolConfig`, `createExport`, `listExports`, `getExport`, `getExportDownloadUrl`

**Branding rename:**
- All frontend references to "Cyber NotifAI" replaced with "Forentis AI"
- Backend default app name changed to "Forentis AI"
- Infrastructure config (.env.example, docker-compose.yml, PORTS.md) updated

**Design decisions:**
- Followed existing frontend patterns: `@tanstack/react-query` for data fetching, ShadCN Card/Badge components, Tailwind utility classes, `lucide-react` icons
- No new dependencies added — all features built with existing React + Tailwind + ShadCN stack
- API base URL configurable via `VITE_API_URL` environment variable (existing pattern)
- Vite proxy configured for `/projects` routes (matching existing pattern for `/jobs`, `/review`, etc.)
- Tab-based layout for ProjectDetail to organize multiple data views without cluttering the UI

**All 1400 tests passing after Steps 1–8.**

**Phase 5 gate:** All 8 steps complete. Platform renamed to Forentis AI. Projects page provides project management. Protocol configs, catalog, density, and exports accessible per-project. Full pipeline API coverage in frontend. PASSED
