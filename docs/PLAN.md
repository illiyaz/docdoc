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

**Implementation is split into 8 steps. Steps 1–4 are complete. Steps 5–8 are pending.**

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

### Steps 5–8 — Pending

| Step | What | Key files to create/modify |
|---|---|---|
| 5 | Configurable dedup anchors | `app/rra/entity_resolver.py` (modify) |
| 6 | CSV export | `app/export/csv_exporter.py` (new), `app/api/routes/exports.py` (new) |
| 7 | LLM integration (Qwen 2.5 7B via Ollama) | `app/llm/client.py`, `app/llm/prompts.py`, `app/llm/audit.py` (new), `tests/test_llm.py` (new) |
| 8 | Frontend + rename to Forentis AI | `frontend/src/pages/Projects.tsx`, `frontend/src/pages/ProjectDetail.tsx` (new), `frontend/src/App.tsx` (modify) |
