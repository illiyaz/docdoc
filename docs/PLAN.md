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

Evolving Cyber NotifAI into **Forentis AI** — a full breach-analysis platform with Projects, editable Protocols, structured cataloging, density scoring, CSV export, configurable dedup, LLM assist (Qwen 2.5 7B via Ollama, governance-gated), guided protocol forms, and catalog upload/linking. Deterministic pipeline remains primary; LLM is additive only.

**Implementation is split into 10 steps + Step 8b. All steps complete (backend + frontend).**

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

### Step 8b — Job Workflow & Connectivity Fixes (COMPLETE)

Connects the job lifecycle to the project management UI so that jobs are visible, launchable, and trackable from within a project. Updates the pipeline stage model to reflect the full 8-stage architecture.

**Backend endpoints implemented:**

| Method + Path | Response | Notes |
|---|---|---|
| `GET /projects/{id}/jobs` | `[JobSummary, ...]` ordered by `created_at` desc | Returns all `ingestion_runs` where `project_id` matches. Each entry: `id`, `status`, `source_path`, `started_at`, `completed_at`, `created_at`, `document_count`, `duration_seconds`, `error_summary`. |
| `GET /jobs/{id}/status` | `{id, status, project_id, progress_pct, current_stage, stages: [{name, status, started_at, completed_at, error_count}], ...}` | 8-stage pipeline breakdown. Stages: Discovery, Cataloging, PII Detection, PII Extraction, Normalization, Entity Resolution, Quality Assurance, Notification. |
| `POST /jobs/run` | `{job_id, status, project_id, protocol_config_id}` | Updated: creates `IngestionRun` record with `status="pending"`, returns immediately for polling. Accepts `project_id` and `protocol_config_id`. SSE streaming moved to `POST /jobs/run/stream`. |
| `GET /jobs/recent?unlinked=true&limit=50` | `[JobSummary, ...]` | Recent jobs optionally filtered to unlinked (no `project_id`). Default limit 50, max 200. |
| `PATCH /jobs/{id}` | Updated job summary | Associates job with a project. 404 if job/project not found. 409 if already linked to a *different* project. Idempotent for same project. |

**Pipeline stages (8-stage architecture):**

| Stage | Task module | Display name |
|---|---|---|
| 1 | `tasks/discovery.py` | Discovery |
| 2 | `tasks/cataloger.py` | Cataloging |
| 3 | `tasks/detection.py` | PII Detection |
| 4 | `tasks/extraction.py` | PII Extraction |
| 5 | `tasks/normalization.py` | Normalization |
| 6 | `tasks/rra.py` | Entity Resolution |
| 7 | `tasks/qa.py` | Quality Assurance |
| 8 | `tasks/notification.py` | Notification |

**Files modified:**

| File | Changes |
|---|---|
| `app/api/routes/jobs.py` | 4 new endpoints (`GET /jobs/recent`, `GET /jobs/{id}/status`, `PATCH /jobs/{id}`, `POST /jobs/run` updated). `PIPELINE_STAGES` constant, `_build_stage_status()`, `_ingestion_run_summary()` helpers. SSE streaming moved to `/jobs/run/stream`. |
| `app/api/routes/projects.py` | `GET /projects/{id}/jobs` endpoint + `_job_summary()` helper. |
| `tests/test_api.py` | 32 new tests across 5 classes: `TestProjectJobs` (6), `TestJobStatus` (9), `TestRunJobPolling` (5), `TestRecentJobs` (6), `TestPatchJob` (6). |
| `CLAUDE.md` | Step 8b marked COMPLETE, test count updated to 1435. |

**Frontend implementation (Step 8b frontend):**

| Feature | Details |
|---|---|
| Jobs tab in ProjectDetail | New tab between Catalog and Density. Table with short ID, status badge, created date, doc count, duration. Clickable rows expand to show 8-stage pipeline progress with polling. |
| Run New Job button | Protocol selector dropdown (base protocols + project protocol configs). Calls `POST /jobs/run` with `project_id` and `protocol_config_id`. |
| Link Existing Job | Dropdown populated from `GET /jobs/recent?unlinked=true`. Shows date and doc count, not raw UUIDs. Calls `PATCH /jobs/{id}` to link. |
| Pipeline progress component | 8-stage stepper (Discovery, Cataloging, PII Detection, PII Extraction, Normalization, Entity Resolution, Quality Assurance, Notification). Polls `GET /jobs/{id}/status` every 3s while running. Progress bar with percentage. |
| JobSubmit project selection | Required project dropdown populated from `GET /projects`. After completion, shows link to project Jobs tab. `project_id` included in submission body. |
| Auto-refresh on job completion | Catalog and Density tabs invalidate react-query cache when a job linked to the project transitions to completed status. |
| Updated pipeline stages | `JobSubmit.tsx` PipelineStepper updated from 5 stages to 8 stages matching backend architecture. |

**Files modified (frontend):**

| File | Changes |
|---|---|
| `frontend/src/api/client.ts` | 7 new interfaces (`JobSummary`, `PipelineStageStatus`, `JobPipelineStatus`, `RunJobBody`, `RunJobResponse`, `PatchJobBody`). 5 new API functions (`getProjectJobs`, `getJobPipelineStatus`, `runJob`, `getRecentJobs`, `linkJobToProject`). |
| `frontend/src/pages/ProjectDetail.tsx` | New `JobsTab` component with job table, `PipelineProgressView` with polling, `StageIcon` helper, run/link job forms. Tab type extended to include "jobs". `handleJobCompleted` callback for auto-refresh. |
| `frontend/src/pages/JobSubmit.tsx` | Required project selection dropdown. Pipeline stages updated to 8. `project_id` passed in submission body. Post-completion link to project. |
| `frontend/vite.config.ts` | Added `/protocols` proxy rule for base protocols API. |

**Remaining frontend work (not yet implemented):**
- Review queue filtering by project

**All 1435 tests passing after Step 8b.**

### Step 9 — Guided Protocol Form (COMPLETE)

**Modified file: `frontend/src/pages/ProjectDetail.tsx`**

Replaced the raw Config JSON textarea in the Protocols tab with a guided, multi-section form (`ProtocolCreateForm` component).

**New constants and presets:**

| Constant | Details |
|---|---|
| `BASE_PROTOCOLS` | 6 protocol presets: hipaa, gdpr, ccpa, pci_dss, state_breach, custom |
| `ENTITY_TYPE_GROUPS` | 14 entity types in 3 categories (Identity, Financial, Health) |
| `DEDUP_ANCHORS` | 5 anchor types: ssn, email, phone, address, name |
| `DEFAULT_EXPORT_FIELDS` | 7 default CSV export columns |
| `PROTOCOL_DEFAULTS` | Per-protocol default configurations for all 6 base protocols |

**Form sections (8 total):**

1. **Name** — Required text input
2. **Base Protocol dropdown** — Selecting a protocol pre-populates all fields with defaults
3. **Target Entity Types** — Checkboxes grouped by category (Identity: 7, Financial: 3, Health: 4)
4. **Confidence Threshold** — Range slider 0.50–1.00 with live display
5. **Dedup Anchors** — Multi-select checkboxes
6. **Sampling Config** — Rate (%), min, max number inputs
7. **Storage Policy** — Strict vs Investigation radio buttons
8. **Export Fields** — Reorderable list with up/down/remove + add custom field
9. **Show raw JSON** — Toggle with read-only preview and raw edit override mode

**Design decisions:**
- No new dependencies — built with existing ShadCN, Tailwind, lucide-react
- Backward compatible — API still receives `{name, base_protocol_id, config_json}`
- Power users can bypass the form via raw JSON edit mode
- TypeScript type-check and production build both pass

### Step 10 — Catalog Tab + Base Protocols (COMPLETE)

**Catalog Tab overhaul (`frontend/src/pages/ProjectDetail.tsx`):**

| Feature | Details |
|---|---|
| File upload zone | Drag-and-drop with progress bar, supported file type indicators, folder recursion. Calls `POST /jobs/upload` with `project_id`. |
| Link Server Path | Input + Scan button for air-gapped deployments with server-side files. |
| Run New Job | Protocol selector (from project configs + base protocols), calls `POST /jobs/run` with `project_id` and `protocol_config_id`. |
| Link Existing Job | Job ID input to associate unlinked jobs with the project. |
| Structure class breakdown | Color-coded cards (structured/semi-structured/unstructured/non-extractable) below catalog summary stats. |

**New backend endpoint:**

| Method + Path | Response | Notes |
|---|---|---|
| `GET /protocols/base` | `[{protocol_id, name, jurisdiction, regulatory_framework, notification_deadline_days}, ...]` | Returns all available base protocol IDs from `config/protocols/*.yaml` |

Added in `app/api/routes/protocols.py` as `base_router`, registered in `app/api/main.py`.

**New YAML protocol files:**

| File | Protocol | Jurisdiction | Key triggers | Deadline |
|---|---|---|---|---|
| `config/protocols/bipa.yaml` | Illinois BIPA (740 ILCS 14) | Illinois, US | BIOMETRIC, FINGERPRINT, FACE_GEOMETRY, IRIS_SCAN, VOICEPRINT, US_SSN | 30 days |
| `config/protocols/dpdpa.yaml` | India DPDPA 2023 | India | AADHAAR, PAN_CARD, PERSON, EMAIL, PHONE, PASSPORT | 72 hours |

Total built-in protocols: **8** (hipaa, gdpr, ccpa, hitech, ferpa, state_breach_generic + bipa, dpdpa).

**Frontend API additions (`client.ts`):**
- `BaseProtocol` interface + `getBaseProtocols()` function
- Protocol dropdown dynamically populated from `GET /protocols/base` with fallback constants

**Tests:** `tests/test_api.py` — 3 new tests (`TestBaseProtocols`): all protocols present, new protocols included, response shape. `tests/test_protocols.py` updated for 8 protocols.

**All 1403 tests passing after Steps 1–10.**

---

### Step 11 — Document Structure Analysis (DSA) (COMPLETE)

**Goal:** Add a pre-detection analysis stage that understands document context — identifying document types, detecting sections, and attributing PII to person roles (primary subject vs institutional vs secondary contact).

**New files:**

| File | Purpose |
|---|---|
| `app/structure/__init__.py` | Package init |
| `app/structure/models.py` | Dataclasses: `DocumentStructureAnalysis`, `SectionAnnotation`, `EntityRoleAnnotation`; type literals for `DocumentType` (9 types), `SectionType` (13 types), `EntityRole` (5 roles) |
| `app/structure/heuristics.py` | `HeuristicAnalyzer` — deterministic doc type classification via keyword density, section detection via heading patterns + column headers, entity role assignment via section mapping |
| `app/structure/protocol_relevance.py` | `PROTOCOL_TARGET_ROLES` mapping + `get_role_relevance()` — maps 8 protocols to target/deprioritize/non-target per role |
| `app/structure/masking.py` | `mask_text_for_llm()` — replaces SSN/email/phone/CC patterns with `[SSN]`/`[EMAIL]`/`[PHONE]`/`[CREDIT_CARD]` placeholders |
| `app/structure/llm_analyzer.py` | `LLMStructureAnalyzer` — sends masked excerpts to Ollama, parses JSON, `merge_analyses()` combines with heuristic (heuristic wins on conflict) |
| `app/tasks/structure_analysis.py` | `StructureAnalysisTask` — pipeline task, runs after cataloger, before detection |
| `alembic/versions/0006_document_structure_analysis.py` | Migration: `documents.structure_analysis` (JSON), `extractions.entity_role` (VARCHAR(32)), `extractions.entity_role_confidence` (Float) |
| `tests/test_structure_analysis.py` | 64 tests across 13 test classes |

**Modified files:**

| File | Change |
|---|---|
| `app/db/models.py` | Added `structure_analysis` to Document, `entity_role` + `entity_role_confidence` to Extraction |
| `app/tasks/detection.py` | Added `entity_role`/`entity_role_confidence` to DetectionResult, `annotate_results_with_structure()` function, `structure` param on `DetectionTask.run()` |
| `app/pii/layer2_context.py` | Added `entity_role` param to `classify()` — institutional reduces score by 0.15, primary_subject boosts by 0.05 |
| `app/rra/entity_resolver.py` | Added `entity_role` to `PIIRecord`, cross-role merge prevention (primary+institutional=0.0, primary+provider=0.0) |
| `app/llm/prompts.py` | Added `ANALYZE_DOCUMENT_STRUCTURE` template, updated `PROMPT_TEMPLATES` dict (now 4 entries) |
| `app/pipeline/dag.py` | Updated stage order to include StructureAnalysisTask as stage 3 |
| `tests/test_schema.py` | Asserts `structure_analysis`, `entity_role`, `entity_role_confidence` columns exist |
| `tests/test_llm.py` | Updated template registry tests for 4 templates |

**Key design decisions:**
- Annotation overlay: DSA produces a separate analysis object, does not mutate ExtractedBlocks
- Deprioritize, not discard: non-target roles get reduced confidence, never zero
- Heuristic wins on conflict: LLM suggestions are additive only
- Nullable columns: all new schema fields nullable, existing data unaffected
- Cross-role merge prevention: primary_subject + institutional/provider = 0.0 confidence (never merge)

**All 1499 tests passing after Step 11.**

---

### Step 12 — Two-Phase Pipeline: Analyze → Review → Extract (COMPLETE)

**Goal:** Add a two-phase pipeline workflow where documents are first analyzed (content onset detection + sample PII extraction from first page), shown to a reviewer for confirmation, then fully extracted after approval. Enables reviewers to validate extraction approach before processing 1000+ page documents.

**Workflow:**
1. **Phase 1 (Analyze):** Discovery → Cataloging → Structure Analysis → Content Onset Detection → Sample Extraction (first content page) → Auto-Approve Check → Job pauses in `analyzed` state
2. **Review:** Reviewer sees per-document analysis cards (document type, onset page, sample PII masked). Approve/Reject/Approve-All. Auto-approve for high-confidence docs.
3. **Phase 2 (Extract):** Full PII detection on ALL pages of approved docs → Entity Resolution → Deduplication → Notification → Complete

**Migration 0007 (`alembic/versions/0007_two_phase_pipeline.py`):**
- New table: `document_analysis_reviews` (11 columns: id, document_id FK, ingestion_run_id FK, status, reviewer_id, rationale, auto_approve_reason, sample_confidence_avg/min, reviewed_at, created_at)
- Extended `documents`: `analysis_phase_status`, `sample_onset_page`, `sample_extraction_count`
- Extended `ingestion_runs`: `pipeline_mode` (full|two_phase), `analysis_completed_at`
- Extended `extractions`: `is_sample` (Boolean, default=False)
- **18 total tables**

**New files:**

| File | Purpose |
|---|---|
| `app/pipeline/content_onset.py` | Generalized onset detection for all file types (PDF delegates to `find_data_onset`, tabular=0, prose scans for ONSET_SIGNALS) |
| `app/pipeline/auto_approve.py` | `should_auto_approve()` — confidence-based + protocol-configurable auto-approve logic |
| `app/pipeline/two_phase.py` | `analyze_generator()` and `extract_generator()` — SSE streaming generators for both phases |
| `app/api/routes/analysis_review.py` | Review endpoints: GET analysis results, POST approve/reject/approve-all |
| `tests/test_two_phase.py` | 21 tests: content onset (7), sample filtering (4), auto-approve (10) |

**API endpoints added:**

| Method | Path | Description |
|---|---|---|
| `POST` | `/jobs/analyze/stream` | SSE Phase 1 — analyze pipeline |
| `POST` | `/jobs/{id}/extract/stream` | SSE Phase 2 — extract pipeline |
| `GET` | `/jobs/{id}/analysis` | Get analysis results per document |
| `POST` | `/jobs/{id}/documents/{doc_id}/approve` | Approve document |
| `POST` | `/jobs/{id}/documents/{doc_id}/reject` | Reject document |
| `POST` | `/jobs/{id}/approve-all` | Batch approve all pending |

**Frontend changes:**
- `client.ts`: New types (`AnalysisReviewDetail`, `SampleExtraction`), 5 new API functions, `submitJobStreaming` routes to `/jobs/analyze/stream` when `pipeline_mode=two_phase`
- `ProjectDetail.tsx`: Pipeline mode toggle ("Analyze First" / "Full Pipeline") in CatalogTab, `AnalysisReviewPanel` component in JobsTab (per-doc cards with sample PII, approve/reject, approve-all, start extraction with SSE progress), `analyzed`/`extracting` status badges

**Tests:** 28 new tests (21 in `test_two_phase.py` + 7 in `test_api.py` `TestAnalysisReview`). **1530+ tests passing.**

**Phase 5 gate (Steps 1–12):** Steps 1–12 + Step 8b complete. 1530+ tests passing. Platform renamed to Forentis AI. Full project management with guided protocol configuration, catalog upload/linking, density scoring, CSV export, governance-gated LLM, job workflow APIs, job management UI, document structure analysis, and two-phase analyze-review-extract pipeline. 8 built-in regulatory protocols. PASSED

---

### Step 13 — LLM Entity Relationship Analysis (COMPLETE)

**Goal:** Elevate the LLM from a document structure classifier to the "understanding brain" of the pipeline. The LLM should read document content, understand entity relationships (which PII belongs to which person), and present this understanding to a human reviewer for confirmation before full extraction.

**Current gaps:**
1. The LLM currently only classifies document type/sections/roles from masked excerpts. It has no awareness of detected PII, entity relationships, or grouping decisions.
2. **Content onset detection is heuristic-only.** The current `find_data_onset()` uses keyword patterns ("name", "SSN", "account") to guess where PII starts. It does NOT run actual PII detection (Presidio) to verify. This means:
   - A disclaimer page mentioning "SSN" in legal text triggers a false onset
   - A document where PII starts on page 10 but no keywords appear until page 10 works — but if "account" appears on page 2 in a table of contents, the system incorrectly samples page 1 (cover page)
   - The `max(0, page-1)` logic can land on a blank/irrelevant page

**New workflow (three-phase):**
1. **Phase 1 — Analyze:** Discovery → Cataloging → Structure Analysis → **PII-Verified Onset Detection** → Sample PII Detection → **LLM Entity Relationship Analysis** → Present to reviewer
2. **Phase 1.5 — Review:** Reviewer sees LLM's understanding: document structure, detected entities, proposed entity groups (which PII belongs to which person), relationship confidence. Reviewer confirms/adjusts entity groups.
3. **Phase 2 — Extract:** Full PII detection on all pages, seeded by confirmed entity groups from Phase 1.5.

#### 13-onset. PII-Verified Onset Detection (Smart Onset)

**Problem:** Current onset detection uses text keyword heuristics only. For a 1000-page PDF where actual PII starts on page 47, the heuristic may incorrectly identify page 3 (which mentions "account" in a disclaimer) as the onset. The sample extraction then runs on pages 2-3, finds zero PII, and the document gets flagged for review unnecessarily.

**Solution: Two-pass onset detection.**

**File: `app/pipeline/content_onset.py`** — New function `find_verified_onset()`:

```
Pass 1 (Heuristic — fast, existing logic):
  Scan pages for ONSET_SIGNALS text patterns.
  Returns list of candidate pages (up to 5 candidates).

Pass 2 (PII Verification — targeted):
  For each candidate page (and the page after it):
    Run PresidioEngine.analyze() on that page's blocks.
    If ≥1 high-confidence PII detection (score ≥ 0.70) found → this is the verified onset.

  If no candidates had PII: scan pages sequentially from page 0,
    running Presidio on each page until PII is found (capped at first 20 pages).
    This handles documents with no keyword signals but real PII data.

  If still no PII found in first 20 pages → onset = 0 (fall back to beginning).
```

**Key design decisions:**
- Pass 1 is cheap (text pattern matching only) — narrows 1000 pages to ~5 candidates
- Pass 2 runs Presidio on at most ~10-15 pages (5 candidates × 2 pages each, or 20 sequential scan) — still fast
- Memory-safe: uses `fitz_doc._forget_page()` after each page (existing pattern)
- For tabular files (CSV/Excel), onset is always 0 — no change needed
- The verified onset page is stored in `documents.sample_onset_page` (existing column)

**File: `app/pipeline/two_phase.py`** — Update `analyze_generator` to use `find_verified_onset()` instead of `find_data_onset()`.

#### 13a. LLM Entity Analysis Prompt

**File: `app/llm/prompts.py`** — New prompt template `ANALYZE_ENTITY_RELATIONSHIPS`:

Input to LLM:
- Document excerpt from the **verified onset page** (raw text when `pii_masking_enabled=false`, masked when true)
- List of PII detections from sample extraction on that page: `[{type: "US_SSN", value: "***-**-6789", page: 3, confidence: 0.95}, ...]`
- Document structure analysis (doc type, sections, entity roles)

LLM asked to produce:
```json
{
  "document_summary": "Payroll records for 3 employees at Acme Corp",
  "entity_groups": [
    {
      "group_id": "G1",
      "label": "Kristin B Aleshire (Employee)",
      "role": "primary_subject",
      "confidence": 0.92,
      "members": [
        {"pii_type": "PERSON", "value_ref": "Kristin B Aleshire", "page": 3},
        {"pii_type": "US_SSN", "value_ref": "***-**-6789", "page": 3},
        {"pii_type": "PHONE_US", "value_ref": "***-***-4567", "page": 3},
        {"pii_type": "DATE_OF_BIRTH_MDY", "value_ref": "01/15/1985", "page": 3}
      ],
      "rationale": "Name, SSN, phone, and DOB appear in same employee record section on page 3"
    },
    {
      "group_id": "G2",
      "label": "Acme Corporation (Employer)",
      "role": "institutional",
      "confidence": 0.98,
      "members": [
        {"pii_type": "ORGANIZATION", "value_ref": "Acme Corporation", "page": 1},
        {"pii_type": "PHONE_US", "value_ref": "***-***-8900", "page": 1}
      ],
      "rationale": "Company name and main phone on letterhead/header"
    }
  ],
  "relationships": [
    {"from": "G1", "to": "G2", "type": "employed_by", "confidence": 0.95}
  ],
  "estimated_unique_individuals": 3,
  "extraction_guidance": "Each page contains one employee record. PII block repeats per employee: name, SSN, address, phone, DOB, salary."
}
```

#### 13b. Entity Group Model

**File: `app/structure/entity_groups.py`** (new):

```python
@dataclass
class EntityGroupMember:
    pii_type: str
    value_ref: str          # masked or raw depending on policy
    page: int | None
    confidence: float

@dataclass
class EntityGroup:
    group_id: str
    label: str              # human-readable label (e.g., "John Smith (Patient)")
    role: str               # primary_subject | institutional | provider | secondary_contact
    confidence: float
    members: list[EntityGroupMember]
    rationale: str          # LLM's reasoning for this grouping
    detected_by: str        # "llm" | "heuristic" | "llm+heuristic"

@dataclass
class EntityRelationship:
    from_group: str
    to_group: str
    relationship_type: str  # employed_by | patient_of | parent_of | emergency_contact_for
    confidence: float

@dataclass
class EntityRelationshipAnalysis:
    document_id: str
    document_summary: str
    entity_groups: list[EntityGroup]
    relationships: list[EntityRelationship]
    estimated_unique_individuals: int
    extraction_guidance: str
```

#### 13c. LLM Entity Analyzer

**File: `app/structure/llm_entity_analyzer.py`** (new):

- `LLMEntityAnalyzer.analyze(blocks, sample_detections, structure_analysis, document_id)` → `EntityRelationshipAnalysis`
- Builds prompt from document excerpt + PII detection list + structure analysis
- Sends to Ollama via `OllamaClient.generate()`
- Parses JSON response into `EntityRelationshipAnalysis`
- Falls back gracefully: if LLM fails, returns None (heuristic-only grouping)

#### 13d. Pipeline Integration

**File: `app/pipeline/two_phase.py`** — Extend `analyze_generator`:

After sample extraction, add new stage `entity_analysis`:
1. Collect sample detections per document
2. Call `LLMEntityAnalyzer.analyze()` with blocks + detections + structure analysis
3. Store `EntityRelationshipAnalysis` on document (JSON column or new table)
4. Include entity groups in the analysis review API response
5. SSE event: `{"stage": "entity_analysis", "status": "complete", "message": "Found 3 unique individuals"}`

Updated analyze stages: `discovery → cataloging → structure_analysis → sample_extraction → entity_analysis → auto_approve → complete`

#### 13e. Analysis Review API Extension

**File: `app/api/routes/analysis_review.py`** — Extend GET `/jobs/{id}/analysis` response:

```json
{
  "document_id": "uuid",
  "file_name": "payroll.pdf",
  "document_summary": "Payroll records for 3 employees at Acme Corp",
  "entity_groups": [
    {
      "group_id": "G1",
      "label": "Kristin B Aleshire (Employee)",
      "role": "primary_subject",
      "confidence": 0.92,
      "members": [...],
      "rationale": "Name, SSN, phone appear in same section"
    }
  ],
  "relationships": [...],
  "estimated_unique_individuals": 3,
  "extraction_guidance": "Each page = one employee record",
  "sample_extractions": [...],
  "review_status": "pending_review"
}
```

#### 13f. Frontend Entity Review Panel

**File: `frontend/src/pages/ProjectDetail.tsx`** — Extend `AnalysisReviewPanel`:

- Show LLM's `document_summary` at top of each document card
- Show entity groups as collapsible cards:
  - Group label + role badge + confidence score
  - Members listed (pii_type + masked value + page)
  - LLM rationale displayed
  - Relationship lines between groups
- Show `extraction_guidance` to help reviewer understand extraction plan
- Show `estimated_unique_individuals` count
- Approve/Reject at entity group level (future: merge/split controls)

#### 13g. Schema Extension (if needed)

**Option A (minimal):** Store `EntityRelationshipAnalysis` as JSON on `documents.entity_analysis` column (new column, migration 0008)

**Option B (full):** New tables `entity_groups` + `entity_group_members` + `entity_relationships` with FKs to documents — enables per-group approval tracking

Start with Option A for rapid iteration; evolve to Option B when entity-level approval is needed.

#### Execution Order

1. **13-onset**: PII-verified onset detection (two-pass: heuristic candidates → Presidio verification)
2. **13a**: New LLM prompt template (`ANALYZE_ENTITY_RELATIONSHIPS`)
3. **13b**: Entity group data models (`entity_groups.py`)
4. **13c**: LLM entity analyzer (calls Ollama with onset page content + PII detections, parses response)
5. **13d**: Pipeline integration (update `analyze_generator`: verified onset → sample extraction → entity analysis)
6. **13e**: API response extension (return entity groups + document summary in analysis review)
7. **13f**: Frontend entity review panel (entity group cards, relationship display, extraction guidance)
8. **13g**: Schema extension (migration 0008 if needed)

#### Updated Analyze Pipeline Stages

```
discovery → cataloging → structure_analysis → verified_onset → sample_extraction → entity_analysis → auto_approve → complete
```

| Stage | What happens | Tool used |
|---|---|---|
| `discovery` | Find files in source directory | FilesystemConnector |
| `cataloging` | Classify file structure | CatalogerTask |
| `structure_analysis` | Doc type, sections, entity roles | Heuristic + LLM (additive) |
| `verified_onset` | **NEW** — Find true first PII page via two-pass: heuristic candidates → Presidio verification | ONSET_SIGNALS + PresidioEngine |
| `sample_extraction` | Run Presidio on verified onset page, store sample Extraction records | PresidioEngine |
| `entity_analysis` | **NEW** — LLM reads onset page + PII detections, proposes entity groups with rationale | OllamaClient |
| `auto_approve` | Confidence-based + protocol-configurable approval decision | should_auto_approve() |

#### Key Constraints

- PII-verified onset is deterministic (Presidio, not LLM) — works without LLM
- LLM entity analysis is **additive** — Presidio/spaCy remains the primary PII detector
- LLM failure → graceful fallback (current behavior: heuristic grouping, Presidio-only detection)
- `pii_masking_enabled` controls whether LLM sees raw or masked PII values
- All LLM calls audit-logged via `llm_call_logs` table
- Cross-role merge prevention still enforced (primary_subject + institutional = never merge)
- Air-gap safe: local Ollama only
- Memory-safe: `_forget_page()` after each page scan during onset verification

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

### Step 16 — Dashboard Redesign (PENDING)

**Goal:** Transform the dashboard from a dead-end page into the command center of the application. The dashboard should answer three questions in under 5 seconds: "What needs my attention?", "What's running?", and "What's the overall state?"

**Current state:** Dashboard shows generic content with no actionable information.

#### 16a. Dashboard Sections

**Layout (top to bottom):**

```
┌─────────────────────────────────────────────────────────────────┐
│ Forentis AI                                        [+ New Project]│
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────┐│
│ │ 3            │ │ 2            │ │ 5            │ │ 1,247    ││
│ │ Active       │ │ Pending      │ │ Jobs This    │ │ Documents││
│ │ Projects     │ │ Reviews      │ │ Week         │ │ Processed││
│ └──────────────┘ └──────────────┘ └──────────────┘ └──────────┘│
│                                                                 │
│ ⚠️  Needs Attention (2)                                         │
│ ┌───────────────────────────────────────────────────────────────┐│
│ │ 📋 Elmer breach test — 1 document pending review    [Review] ││
│ │ 📋 Client XYZ breach — 3 documents pending review   [Review] ││
│ └───────────────────────────────────────────────────────────────┘│
│                                                                 │
│ 🏃 Running Jobs (1)                                             │
│ ┌───────────────────────────────────────────────────────────────┐│
│ │ ⏳ Client XYZ breach — Extracting (45%) — 12 docs   [View]   ││
│ └───────────────────────────────────────────────────────────────┘│
│                                                                 │
│ 📊 Active Projects                                              │
│ ┌───────────────────────────────────────────────────────────────┐│
│ │ Elmer breach test    active    12 docs   Last: 2 min ago     ││
│ │ Client XYZ breach    active    45 docs   Last: 1 hour ago    ││
│ │ Q4 Incident review   active    3 docs    Last: 3 days ago    ││
│ └───────────────────────────────────────────────────────────────┘│
│                                                                 │
│ 📜 Recent Activity                                              │
│ ┌───────────────────────────────────────────────────────────────┐│
│ │ 2 min ago   Job completed — Elmer breach test (1 doc)        ││
│ │ 15 min ago  Document approved — Client XYZ (payroll.pdf)     ││
│ │ 1 hour ago  Export generated — Client XYZ (CSV, 45 subjects) ││
│ │ 3 hours ago Project created — Q4 Incident review             ││
│ └───────────────────────────────────────────────────────────────┘│
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

#### 16b. Backend: Dashboard API

**File: `app/api/routes/dashboard.py`** (new):

```
GET /dashboard/summary
```

Response:

```json
{
  "stats": {
    "active_projects": 3,
    "pending_reviews": 2,
    "jobs_this_week": 5,
    "documents_processed": 1247
  },
  "needs_attention": [
    {
      "type": "pending_review",
      "project_id": "uuid",
      "project_name": "Elmer breach test",
      "pending_count": 1,
      "oldest_pending_at": "2026-03-06T19:18:34Z"
    }
  ],
  "running_jobs": [
    {
      "job_id": "uuid",
      "project_id": "uuid",
      "project_name": "Client XYZ breach",
      "status": "extracting",
      "progress_pct": 45,
      "document_count": 12,
      "started_at": "2026-03-06T18:30:00Z"
    }
  ],
  "active_projects": [
    {
      "id": "uuid",
      "name": "Elmer breach test",
      "status": "active",
      "document_count": 12,
      "last_activity_at": "2026-03-06T19:18:34Z",
      "pending_reviews": 1,
      "completed_jobs": 3
    }
  ],
  "recent_activity": [
    {
      "type": "job_completed",
      "project_name": "Elmer breach test",
      "detail": "1 document analyzed",
      "timestamp": "2026-03-06T19:18:34Z"
    },
    {
      "type": "document_approved",
      "project_name": "Client XYZ breach",
      "detail": "payroll.pdf approved",
      "timestamp": "2026-03-06T19:03:00Z"
    }
  ]
}
```

**Query strategy:**

The dashboard endpoint aggregates from existing tables — no new tables needed:

| Stat | Query |
|---|---|
| `active_projects` | `SELECT COUNT(*) FROM projects WHERE status='active'` |
| `pending_reviews` | `SELECT COUNT(*) FROM document_analysis_reviews WHERE status='pending_review'` joined with project name |
| `jobs_this_week` | `SELECT COUNT(*) FROM ingestion_runs WHERE created_at > now()-7days` |
| `documents_processed` | `SELECT COUNT(*) FROM documents` |
| `running_jobs` | `SELECT * FROM ingestion_runs WHERE status IN ('pending','running','analyzing','extracting')` joined with project |
| `active_projects` list | `SELECT * FROM projects WHERE status='active' ORDER BY updated_at DESC LIMIT 10` with aggregated counts |
| `recent_activity` | Union of: recent job completions, recent approvals, recent exports, recent project creations. Ordered by timestamp DESC, LIMIT 20. |

#### 16c. Frontend: Dashboard Page

**File: `frontend/src/pages/Dashboard.tsx`** — Complete redesign:

**Components:**

| Component | Data source | Interaction |
|---|---|---|
| `StatCards` | `stats` from API | Clickable: active projects → /projects, pending reviews → filtered review list |
| `NeedsAttention` | `needs_attention` from API | Each item has [Review] button → navigates to project Jobs tab |
| `RunningJobs` | `running_jobs` from API | Each item has [View] button → navigates to project Jobs tab. Auto-refreshes every 10s while jobs are running. |
| `ActiveProjects` | `active_projects` from API | Compact table/cards. Clickable rows → project detail. Shows doc count, last activity, pending review count. |
| `RecentActivity` | `recent_activity` from API | Timeline feed. Each entry shows icon (by type), project name, detail, relative timestamp. |

**Auto-refresh:** Dashboard polls `GET /dashboard/summary` every 30 seconds. When running jobs exist, polls every 10 seconds. Stops polling when tab is not visible (page visibility API).

**Empty states:**
- No projects yet → "Create your first project to get started" + [New Project] button
- No pending reviews → "All clear — no documents waiting for review"
- No running jobs → section hidden entirely
- No recent activity → "No activity yet"

**"New Project" button:** Top-right corner, always visible. Opens inline form or navigates to /projects with create form focused.

#### 16d. Sidebar Navigation Update

**File: `frontend/src/App.tsx`** — Update sidebar:

Current sidebar has: Dashboard, Projects, Low Confidence, Escalation, QC Sampling, RRA Review, Submit Job, Diagnostic.

Some of these are legacy/disconnected. Update:

```
Sidebar (updated):
├── Dashboard          ← redesigned (Step 16)
├── Projects           ← existing (works well)
├── Review Queue       ← consolidate Low Confidence + Escalation + QC Sampling + RRA Review
│                        into a single page with tab/filter controls
├── Submit Job         ← keep, but with project selection (Step 8b)
└── Settings           ← new: app config, LLM settings, about
```

The 4 separate review queue pages (Low Confidence, Escalation, QC Sampling, RRA Review) should become tabs or filters on a single "Review Queue" page. This reduces sidebar clutter and makes navigation clearer. However, this is a larger refactor — for Step 16, just update Dashboard and add the pending reviews link. The sidebar consolidation can be a future step.

#### 16e. Execution Prompt

```
@agent-general-purpose Read CLAUDE.md and docs/PLAN.md Step 16 for context.

Step 16: Dashboard redesign.

=== BACKEND ===

1. Create app/api/routes/dashboard.py with a single endpoint:
   GET /dashboard/summary

   Returns JSON with 5 sections:
   - stats: active_projects count, pending_reviews count,
     jobs_this_week count, documents_processed count
   - needs_attention: list of projects with pending document reviews
     (project_id, project_name, pending_count, oldest_pending_at)
   - running_jobs: list of in-progress jobs
     (job_id, project_id, project_name, status, progress_pct,
     document_count, started_at)
   - active_projects: list of active projects with summary stats
     (id, name, status, document_count, last_activity_at,
     pending_reviews, completed_jobs). Order by last_activity DESC.
   - recent_activity: last 20 events across all projects. Union of:
     job completions, document approvals, export generations,
     project creations. Each has: type, project_name, detail, timestamp.

   All data comes from existing tables — no new tables needed.
   Use efficient queries (avoid N+1). Consider a single query with
   joins for active_projects stats.

2. Register dashboard router in app/api/main.py.
   Add /dashboard proxy rule in frontend/vite.config.ts.

=== FRONTEND ===

3. Redesign frontend/src/pages/Dashboard.tsx:

   a) Stat cards row (4 cards):
      Active Projects, Pending Reviews, Jobs This Week, Documents Processed
      Each card is clickable (projects → /projects, reviews → first
      pending project)

   b) Needs Attention section:
      List of projects with pending reviews. Each row shows project
      name, pending count, and [Review] button that navigates to
      that project's Jobs tab.
      Empty state: "All clear — no documents waiting for review"

   c) Running Jobs section:
      List of in-progress jobs with project name, status, progress
      bar, doc count. [View] button navigates to project Jobs tab.
      Hidden when no running jobs.
      Auto-refresh every 10s when running jobs exist.

   d) Active Projects section:
      Compact cards or table rows. Project name, doc count, last
      activity (relative time), pending review count badge.
      Clickable → navigates to project detail.
      Empty state: "Create your first project" + [New Project] button.

   e) Recent Activity feed:
      Timeline with icon per event type (job complete, doc approved,
      export generated, project created). Project name, detail,
      relative timestamp.
      Limit to 20 most recent.

4. Add dashboard API function to frontend/src/api/client.ts:
   - DashboardSummary interface matching API response
   - getDashboardSummary() function
   - Auto-polling with react-query refetchInterval (30s default,
     10s when running jobs exist)

5. Use existing ShadCN components (Card, Badge, Button) and
   lucide-react icons. Follow existing Tailwind patterns.
   No new dependencies.

=== TESTS ===

6. Create tests/test_dashboard.py:
   - Empty state: no projects → all counts zero, empty lists
   - With projects: counts correct
   - Pending reviews: shows in needs_attention
   - Running job: shows in running_jobs with progress
   - Recent activity: job completion event appears
   - Recent activity: ordered by timestamp DESC
   - Activity limit: max 20 entries returned
   - Active projects: ordered by last_activity DESC

7. Run pytest on all changed files. Fix failures up to 3 attempts.
   Update CLAUDE.md.
```