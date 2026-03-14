# CLAUDE.md — Forentis AI

Single source of truth for how this codebase is built and maintained. All contributors (human and AI) must follow these rules without exception.

See [docs/PLAN.md](docs/PLAN.md) for active implementation steps (14-16).
See [docs/PLAN_COMPLETED.md](docs/PLAN_COMPLETED.md) for completed steps (Phases 1-4, Steps 1-13).
See [docs/SCHEMA.md](docs/SCHEMA.md) for detailed technical architecture (PDF processing, PII detection, RRA, protocols, HITL, notifications).

---

## 0) Product Goal (non-negotiable)

**Forentis AI** is an end-to-end breach notification platform. The pipeline has three outcomes:

1. **Identify** — extract PII/PHI/FERPA/SPI from every document in a breach dataset
2. **Resolve** — deduplicate and link records to unique individuals (Rational Relationship Analysis)
3. **Notify** — generate and deliver breach notifications per applicable regulatory protocol

Build an **offline-capable, air-gap-safe** system that is:

- **Evidence-backed** — every extracted value carries page number, character offsets, and bounding box
- **Deterministic first** — rules and heuristics are primary; ML and LLM are additive and optional
- **Scalable to 1000+ page PDFs** — page-streaming architecture, never load full document into memory
- **Checkpointable** — every page processed is persisted; crashed jobs resume from last completed page
- **Safe by default** — STRICT storage policy; no raw PII ever persisted or logged
- **Governance-ready** — every extraction decision must be explainable and auditable
- **Air-gap deployable** — zero runtime network dependencies
- **Protocol-driven** — every job runs against a counsel-approved Protocol
- **Notification-complete** — pipeline ends with email delivery or print-ready postal output

---

## 1) Architecture: Deterministic Pipeline (NOT Agent-Based)

A **Prefect DAG pipeline** of well-defined processing stages. Each stage has typed inputs, typed outputs, and deterministic behavior. No autonomous agents, no LLM orchestration frameworks, no cloud API dependencies.

### Pipeline stages

| Design doc name | What it actually is |
|---|---|
| Discovery Agent | `tasks/discovery.py` — filesystem/DB traversal, document cataloging |
| Structure Analysis Agent | `tasks/structure_analysis.py` — document type, section detection, entity role attribution |
| Document Understanding | `structure/llm_document_understanding.py` — LLM semantic schema (field map, people, dates, suppression hints). Fallback: heuristic + deny-lists |
| PII Detection Agent | `tasks/detection.py` — Presidio + spaCy NER, confidence scoring, post-filtered through DocumentSchema when available |
| PII Extraction Agent | `tasks/extraction.py` — pattern match + context window extraction |
| Normalization Agent | `tasks/normalization.py` — phone/address/name/email normalization |
| RRA Agent | `tasks/rra.py` — entity resolution, deduplication, NotificationSubject building |
| Quality Assurance Agent | `tasks/qa.py` — validation rule set, completeness checks |
| Notification Agent | `tasks/notification.py` — list building, email delivery, print rendering |
| Error Handling Agent | `tasks/error_handler.py` — retry logic, failure categorization, escalation routing |

Each task is a plain Python class. Prefect handles orchestration, scheduling, retries, and observability.

LLM-backed reasoning only in Phase 4+, gated behind `llm_assist_enabled: false`. Never replaces deterministic pipeline.

---

## 2) Technology Stack (Locked — No Substitutions Without Explicit Approval)

### Pipeline & orchestration

| Component | Choice | Rejected alternatives |
|---|---|---|
| Workflow orchestration | **Prefect (self-hosted)** | LangGraph, CrewAI, AutoGen, Airflow |
| PDF engine | **PyMuPDF (fitz)** | pdfplumber, PyPDF2 |
| OCR | **PaddleOCR** | Tesseract |
| Multi-format parsing | **Apache Tika (self-hosted)** | Cloud-based parsers |
| Word documents | **python-docx** | — |

### PII detection & NLP

| Component | Choice |
|---|---|
| PII detection | **Microsoft Presidio** |
| NER / context classification | **spaCy** |
| Custom patterns | **Regex (re module)** |
| Model training tracking | **MLflow (self-hosted)** |

### Infrastructure

| Component | Choice |
|---|---|
| Primary database | **PostgreSQL** |
| Caching + task queuing | **Redis** |
| Document/object storage | **MinIO (self-hosted S3-compatible)** |
| Message queue | **RabbitMQ (self-hosted)** |
| Secret / key management | **HashiCorp Vault (self-hosted)** |
| Observability | **Prometheus + Grafana (self-hosted)** |

### Application

| Component | Choice |
|---|---|
| Backend API | **FastAPI** |
| Frontend (human review UI) | **React + Tailwind + ShadCN** |
| ORM | **SQLAlchemy** |
| DB migrations | **Alembic** |
| DB connector (Postgres) | **psycopg2** |
| DB connector (Mongo) | **pymongo** |

### Air-gap compliance rule

Every library and model must be resolvable from a local artifact registry. No library may make outbound network calls at runtime. Telemetry/phone-home must be disabled.

---

## 3) Project Structure (Canonical)

```
project-root/
├── CLAUDE.md
├── docs/
│   ├── PLAN.md                    # active implementation steps (14-16)
│   ├── PLAN_COMPLETED.md          # completed steps archive (1-13)
│   └── SCHEMA.md                  # detailed technical architecture
├── config/
│   ├── config.yaml                # all environment config, no secrets
│   └── protocols/                 # 8 built-in YAML protocol files
├── app/
│   ├── tasks/                     # pipeline stages (Prefect tasks)
│   │   ├── discovery.py
│   │   ├── structure_analysis.py  # Phase 5 Step 11 — DSA pipeline task
│   │   ├── detection.py
│   │   ├── extraction.py
│   │   ├── cataloger.py           # Phase 5 Step 3
│   │   ├── qa.py
│   │   └── error_handler.py
│   ├── pipeline/
│   │   ├── dag.py                 # Prefect DAG wiring
│   │   ├── two_phase.py           # Two-phase pipeline: analyze_generator + extract_generator
│   │   ├── content_onset.py       # Generalized content onset detection (all file types)
│   │   ├── instance_detector.py   # Step 20: Marker-based instance boundary detection
│   │   └── auto_approve.py        # Auto-approve logic for document analysis review
│   ├── pdf/
│   │   ├── reader.py              # PyMuPDF streaming wrapper
│   │   ├── renderer.py            # Step 20: PDF page-to-image for vision models
│   │   ├── ocr.py                 # PaddleOCR integration
│   │   ├── classifier.py          # digital/scanned/corrupted detection
│   │   ├── onset.py               # content onset detection
│   │   └── stitcher.py            # cross-page tail-buffer logic
│   ├── pii/
│   │   ├── presidio_engine.py     # Presidio wrapper + custom recognizers
│   │   ├── spacy_classifier.py    # context window classification
│   │   ├── layer1_patterns.py     # regex pattern library (85+ patterns)
│   │   ├── layer2_context.py      # Layer 2 context window logic
│   │   ├── layer3_positional.py   # Layer 3 header inference
│   │   ├── context_deny_list.py   # Step 14a: common-word deny-list, reference labels, FP heuristic
│   │   ├── schema_filter.py       # Step 14b: DocumentSchema post-filter for Presidio detections
│   │   └── pattern_validator.py   # Step 20: Post-extraction pattern validation
│   ├── normalization/             # phone, email, name, address normalizers
│   ├── rra/                       # entity resolver, deduplicator, fuzzy matching
│   ├── protocols/                 # Protocol dataclass, loader, registry
│   ├── notification/              # list builder, email sender, print renderer, templates
│   ├── audit/                     # events, audit_log
│   ├── review/                    # roles, queue_manager, workflow, sampling
│   ├── structure/
│   │   ├── models.py              # DSA dataclasses (DocumentStructureAnalysis, etc.)
│   │   ├── heuristics.py          # Deterministic document type/section/role analyzer
│   │   ├── protocol_relevance.py  # Protocol → entity role relevance mapping
│   │   ├── masking.py             # PII masking for LLM prompts (respects pii_masking_enabled)
│   │   ├── llm_analyzer.py        # LLM-assisted structure analysis (additive)
│   │   ├── entity_groups.py       # EntityGroup, EntityRelationship dataclasses (Step 13)
│   │   ├── llm_entity_analyzer.py # LLM entity relationship analysis (Step 13)
│   │   ├── document_schema.py     # Step 14a: DocumentSchema, FieldContext, PersonContext, DateContext
│   │   ├── llm_document_understanding.py  # Step 14b: LLM Document Understanding → DocumentSchema
│   │   ├── llm_template_extractor.py # Step 19: LLM-driven PII extraction for templates
│   │   └── vision_extractor.py    # Step 20: Vision-language model PII extraction
│   ├── llm/
│   │   ├── client.py              # OllamaClient — governance-gated LLM wrapper
│   │   ├── prompts.py             # Prompt templates (classify, assess, suggest, DSA, entity relationships, document understanding)
│   │   └── audit.py               # LLM call logging (log_llm_call, get_llm_calls)
│   ├── core/
│   │   ├── constants.py           # ENTITY_CATEGORY_MAP, DATA_CATEGORIES (8 categories)
│   │   ├── policies.py            # STRICT / INVESTIGATION storage policy
│   │   ├── security.py            # hashing, encryption, EncryptionProvider
│   │   ├── logging.py             # PIISafeFilter
│   │   └── settings.py            # pydantic-settings
│   ├── db/
│   │   ├── models.py              # SQLAlchemy ORM models (18 tables)
│   │   └── repositories.py        # thin data access layer
│   └── api/
│       ├── main.py
│       ├── middleware/
│       └── routes/                # health, diagnostic, jobs, projects, protocols, analysis_review
├── frontend/                      # React Forentis AI UI
│   └── src/
│       ├── api/client.ts          # API client (types + functions)
│       ├── pages/
│       │   ├── Dashboard.tsx      # Review dashboard
│       │   ├── Projects.tsx       # Project list + create
│       │   ├── ProjectDetail.tsx  # Project detail (6 tabs: Overview, Protocols, Catalog, Jobs, Density, Exports)
│       │   ├── QueueView.tsx      # Review queue
│       │   ├── SubjectDetail.tsx  # Subject detail
│       │   ├── JobSubmit.tsx      # Job submission (requires project selection)
│       │   └── Diagnostic.tsx     # Diagnostic scan
│       ├── components/            # Shared components (ShadCN + custom)
│       └── App.tsx                # Routes + sidebar + Forentis AI branding
├── alembic/
│   └── versions/                  # 0001–0010
├── tests/
│   ├── test_schema.py
│   ├── test_repositories.py
│   ├── test_policies.py
│   ├── test_extraction.py
│   ├── test_safety.py             # PII never appears in logs or exceptions
│   ├── test_api.py
│   ├── test_cataloger.py
│   ├── test_constants.py            # entity category mapping coverage
│   ├── test_density.py
│   ├── test_llm.py
│   ├── test_structure_analysis.py   # DSA: doc type, sections, roles, masking, RRA prevention
│   └── test_two_phase.py            # Two-phase pipeline: content onset, auto-approve, review
├── models/                        # pre-packaged spaCy and Presidio models
└── scripts/
    └── retrain.py                 # supervised retraining from human labels
```

---

## 4) Schema Contract

The canonical DB schema (18 tables) is defined by:
- `app/db/models.py`
- `alembic/versions/0001_initial.py` through `0008_entity_analysis.py`
- `tests/test_schema.py` and `tests/test_repositories.py`

### Rules

- Do NOT introduce new tables or columns without updating models.py, migration, test_schema.py, and affected repository tests simultaneously
- If a mismatch exists between models and migration, tests must fail — never suppress this
- Early stage: migration rewrites are allowed. Once the system processes real data, all migrations must be additive only
- All new `project_id` FKs are **nullable** for backward compatibility with pre-project data

---

## 5) Storage Policy & Security

### STRICT mode (default)

- Never store `raw_value` anywhere — not in DB, not in logs, not in exceptions
- `hashed_value` required for every extracted PII element
- `raw_value_encrypted` must be NULL
- Default storage policy: `hash`

### INVESTIGATION mode

- `raw_value_encrypted` allowed (encrypted via Fernet, minimum)
- `retention_until` required and enforced — records auto-expire
- If encryption key missing: fail closed, never fall back to plaintext

### Security (`app/core/security.py`)

- Hashing: `SHA256(tenant_salt + raw_value)` — deterministic, tenant-isolated
- Encryption: Fernet for MVP; pluggable `EncryptionProvider` interface
- No raw PII in any log statement, exception message, stack trace, or debug output — ever

See [docs/SCHEMA.md](docs/SCHEMA.md) for full storage policy contract and security/governance details.

---

## 6) Key Architectural Decisions (Locked)

These are detailed in [docs/SCHEMA.md](docs/SCHEMA.md). Summary:

- **PDF processing:** PyMuPDF page-streaming + PaddleOCR for scanned pages. Dual-path (digital vs scanned). **PII-verified onset detection** (two-pass: heuristic keyword scan → Presidio verification on candidate pages to find true first PII page). Cross-page tail-buffer stitching. Checkpointing per page.
- **PII detection:** Three layers (pattern match → context window → positional header). Presidio + spaCy. 85+ patterns covering PII/PHI/FERPA/SPI/PPRA. 8 data categories (PII, SPII, PHI, PFI, PCI, NPI, FTI, CREDENTIALS) with multi-category mapping per entity type. **Protocol-driven recognizer filtering** — only jurisdiction-relevant recognizers run per protocol (GDPR disables US types, DPDPA disables UK/EU types). **Context deny-lists** suppress common-word false positives (STUDENT_ID "Statement", VAT_EU "Description"). **DocumentSchema filter** (LLM-powered) suppresses/reclassifies detections based on semantic document understanding.
- **LLM Document Understanding:** LLM reads onset page, produces a DocumentSchema (field map, people, dates, table schemas, suppression hints). Schema is a post-filter on Presidio — never modifies Presidio's engine. Table-aware filtering: non-PII table columns suppress all detections from table region, PII columns confirm detections. Reduces false positives from ~85% to ~10-15%. Without LLM, deny-lists + tighter patterns reduce to ~40-50%. One LLM call per document (not per detection).
- **Document Structure Analysis:** Heuristic-first document type classification, section detection, entity role attribution. LLM-assisted analysis additive only (`llm_assist_enabled`). Cross-role merge prevention in RRA (primary_subject + institutional = never merge).
- **LLM Entity Relationship Analysis:** LLM reads document content + sample PII detections, understands which PII belongs to which person, proposes entity groups with confidence + rationale. Presented to human reviewer for confirmation before full extraction. Additive to Presidio/spaCy detection. Graceful fallback when LLM unavailable.
- **Pipeline stage order (analyze phase):** `discovery → cataloging → verified_onset → document_understanding (LLM) → sample_extraction (with schema filter) → entity_analysis → auto_approve`. Without LLM: `discovery → cataloging → verified_onset → structure_analysis (heuristic) → sample_extraction (with deny-lists) → auto_approve`.
- **RRA:** Entity resolution via Union-Find. Confidence-weighted merge signals. Cross-role merge prevention. Threshold: 0.80 auto-accept, 0.60–0.79 human review, <0.60 separate.
- **Protocols:** 8 built-in (HIPAA, GDPR, CCPA, HITECH, FERPA, state_breach_generic, BIPA, DPDPA). YAML-configurable. Selected once per job.
- **HITL:** 4 roles (REVIEWER, LEGAL_REVIEWER, APPROVER, QC_SAMPLER). 4 review queues. State machine: AI_PENDING → HUMAN_REVIEW → LEGAL_REVIEW → APPROVED → NOTIFIED.
- **Notification:** SMTP email + WeasyPrint postal letters. Template-driven. Delivery gated on APPROVED status only.
- **Audit:** Every extraction decision traceable to a specific rule/pattern/classifier. Append-only audit trail.
- **Coordinates:** Evidence-only — never used as search mechanism.

---

## 7) Ground Rules for Code Changes

- Do not introduce new tables/columns without updating `models.py`, migration, `test_schema.py`, and affected repository tests simultaneously
- Prefer small, reviewable diffs — one coherent behavior change per prompt
- Add tests in the same change for every behavior modification
- Never broaden scope beyond the current prompt
- Run before marking any task done:
  ```
  python -m py_compile <changed files>
  pytest tests/test_schema.py tests/test_repositories.py tests/test_safety.py
  ```
- Summarize every change as: files modified + what tests now verify

---

## 8) What NOT to Do

- Do not use LangGraph, CrewAI, AutoGen, or any agent framework
- Do not call any cloud LLM API (OpenAI, Anthropic, Cohere, etc.)
- Do not use Tesseract (use PaddleOCR)
- Do not use pdfplumber or PyPDF2 (use PyMuPDF)
- Do not load entire PDFs into memory
- Do not use fixed coordinates as the primary extraction mechanism
- Do not store or log raw PII values anywhere
- Do not make LLM mandatory for correctness — the deterministic pipeline must work without it
- Do not introduce runtime network dependencies
- Do not broaden scope beyond the active phase

---

## 9) Current Progress

### Phase 1 — Deterministic Core: COMPLETE
### Phase 2 — Normalization + RRA: COMPLETE
### Phase 3 — Protocol Configuration + Notification Delivery: COMPLETE
### Phase 4 — Enhanced HITL + Comprehensive Audit Trail: COMPLETE

**Product is demo-ready. All pitch deck promises are backed by tested code.**

### Phase 5 — Forentis AI Evolution: IN PROGRESS

| Step | Status | Summary |
|---|---|---|
| 1. Schema + Migration | COMPLETE | 5 new tables, 4 extended tables, migration 0005, 17 total tables |
| 2. Project + Protocol API | COMPLETE | CRUD for projects + protocol configs, catalog-summary + density endpoints |
| 3. Cataloger Task | COMPLETE | File structure classifier (structured/semi-structured/unstructured/non-extractable) |
| 4. Density Scoring | COMPLETE | Entity categorization (8 categories: PII/SPII/PHI/PFI/PCI/NPI/FTI/CREDENTIALS), multi-category mapping, confidence aggregation, per-doc + project summaries |
| 5. Configurable dedup anchors | COMPLETE | `active_anchors` param on `build_confidence` + `EntityResolver.resolve`, 6 anchor types, validated input |
| 6. CSV export | COMPLETE | `app/export/csv_exporter.py`, `app/api/routes/exports.py`, masked PII, configurable columns |
| 7. LLM integration | COMPLETE | `app/llm/client.py`, `app/llm/prompts.py`, `app/llm/audit.py` — governance-gated Ollama client, 3 prompt templates, full audit logging, 55 tests |
| 8. Frontend + rename | COMPLETE | Projects list + detail pages, App.tsx routes, rename Cyber NotifAI to Forentis AI across frontend + backend |
| 8b. Job Workflow | COMPLETE | Backend: 5 new endpoints (project jobs, job status, run job, recent jobs, link job). Frontend: Jobs tab in ProjectDetail (table + pipeline progress + run/link), 8-stage pipeline stepper, JobSubmit requires project selection, auto-refresh Catalog/Density on job completion. |
| 9. Guided Protocol Form | COMPLETE | Replaced raw JSON textarea with guided form: base protocol dropdown (6 presets), entity type checkboxes (Identity/Financial/Health), confidence slider, dedup anchor multi-select, sampling config, storage policy radios, reorderable export fields, raw JSON toggle for power users |
| 10. Catalog Tab + Base Protocols | COMPLETE | Catalog tab with file upload (drag-and-drop), server path linking (air-gap), Run New Job, Link Existing Job; GET /protocols/base endpoint; base protocol dropdown populated from API (8 YAML protocols); placeholder YAML for bipa, dpdpa |
| 11. Document Structure Analysis | COMPLETE | Heuristic doc type classification (9 types), section detection (13 section types), entity role attribution (5 roles), protocol relevance mapping (8 protocols), LLM-assisted analysis (additive, governance-gated), cross-role merge prevention in RRA, migration 0006, 64 new tests |
| 12. Two-Phase Pipeline | COMPLETE | Analyze → Review → Extract workflow. Content onset detection (all file types), sample PII extraction from first content page, document-level analysis review (approve/reject/approve-all), auto-approve (confidence-based + protocol-configurable), Phase 2 full extraction on approved docs, migration 0007, `DocumentAnalysisReview` table (18 total), frontend pipeline mode toggle + analysis review panel, 28 new tests |
| 13. LLM Entity Relationship Analysis | COMPLETE | PII-verified onset detection (two-pass: heuristic candidates → Presidio verification). LLM entity relationship analysis: reads onset page + PII detections, proposes entity groups with confidence + rationale. New analyze stages: `verified_onset` + `entity_analysis`. `EntityRelationshipAnalysis` dataclass, `LLMEntityAnalyzer`, `ANALYZE_ENTITY_RELATIONSHIPS` prompt. API returns entity groups/relationships/guidance. Frontend entity group cards with role badges, relationship display, extraction guidance. Migration 0008 (`documents.entity_analysis` JSON column). 20 new tests. |
| 14. LLM Document Understanding & Detection Quality | COMPLETE | Context deny-lists, tighter Presidio patterns, protocol-driven recognizer filtering, LLM Document Understanding (DocumentSchema + SchemaFilter + TableSchema), detection tuning, Catalog tab UX. |
| 15. Field-Level Review + Protocol Mapping | COMPLETE | Two-tier detection toggle, protocol field mapping, `detection_review_decisions` table (migration 0009, 19 tables). |
| 16. UX Consolidation: Dashboard, Jobs, Sidebar, Density | COMPLETE | Dashboard command center, Jobs tab (cancel/archive/filter/pagination), Sidebar 8→5 items, Density state-driven display. |
| 17. Cross-Page Template Linking + FP Cleanup + Auto-Export | COMPLETE | DocumentTemplate, PageRole, multi-page LLM reading, build_composite_record, financial term deny-list, cross-type suppression, auto-CSV-export. |
| 18. Auditor-Ready CSV Export with Lineage | COMPLETE | Schema-driven CSV (auditor/minimal/full), +5 lineage columns on NotificationSubject (migration 0010), gov ID masking, preview endpoint. |
| 19. Schema-Driven LLM Extraction for Templates | COMPLETE | LLMTemplateExtractor, ENTITY_EXTRACTION_GUIDE (17 types), ALWAYS_EXTRACT_IF_PRESENT, 3-path extraction (exclusive), cross-batch dedup, marker-based instance boundaries, 24 tests. |
| 20. Vision-First Extraction Architecture | COMPLETE | Vision-language model as primary extractor. VisionDocumentExtractor, PDF page renderer, instance boundary detector, OllamaClient.generate_with_images. extraction preview with per-field page numbers. 4 extraction strategies: template (1 person/N pages), table (N people/page), vision page, Presidio fallback. Pattern validation (NI/SSN/date/email format checks, financial term + org name suppression). Per-protocol vision_model + vision_page_dpi config. Table extraction: is_tabular + records_per_page_estimate on DocumentSchema, VisionDocumentExtractor.extract_table_pages(), LLMTemplateExtractor.extract_table_pages(), tabular preview with sample rows. 79 new tests. |

**Bugfix: Extraction preview multi-page read** — Preview now reads ALL pages of instance 0 (not just identity page). `build_preview_extraction_prompt()` asks LLM for per-field page numbers (`{value, page}` format). `_parse_preview_response()` parses LLM output with canonical field mapping. Instance count uses `find_instance_boundaries()` when marker set. 11 net new tests.

**CRITICAL Bugfix: Cross-instance dedup over-merging** — 149 unique individuals were being collapsed to 28 rows. Root cause: `_deduplicate_records()` keyed on name only, merging people from DIFFERENT template instances (e.g., "P Davie" on pages 1-3 merged with "P Davies" on pages 4-6). Fix: (1) `_deduplicate_records()` now keys on `(name, page_range)` for template docs (`instance_aware=True`), keeping `name`-only for tables (`instance_aware=False`). (2) `EntityResolver.build_confidence()` returns 0.0 for same-document records with different `page_range` (cross-instance merge prevention). Each template instance = one unique person. 7 net new tests.

**Batch Reliability + Configurable Dedup + Dedup UI** (production fixes):
  - Retry: MAX_RETRIES=3, backoff 2s/4s/8s, split-to-individual on batch failure, unload_unused_models(), timeout_override=120s
  - Configurable dedup: _build_anchor_key() with 5 anchors (ssn, name_dob, email, phone, name_address), wired from protocol config
  - Analysis API returns {documents, dedup_anchors, protocol_name}, frontend shows read-only anchor checkboxes
  - `tests/test_batch_reliability.py`: 30 new tests

**2177 tests passing after Steps 1–20.**

See [docs/PLAN.md](docs/PLAN.md) for active steps and [docs/PLAN_COMPLETED.md](docs/PLAN_COMPLETED.md) for completed reference.

---

## 10) Testing Expectations

- Prefer unit tests + minimal integration tests using SQLite in-memory
- `tests/test_safety.py` runs on every test invocation — not optional
- Tests must validate:
  - **Behavior:** what is stored and what is returned
  - **Schema:** columns exist, defaults are correct, constraints hold
  - **Safety:** no raw PII appears in logs, exceptions, or API responses
  - **Extraction accuracy:** known PII patterns are found; known non-PII is not flagged
- Avoid snapshot tests — assert explicit, named conditions
- STRICT mode tests: assert `raw_value_encrypted IS NULL` on every write
- INVESTIGATION mode tests: assert `retention_until IS NOT NULL` on every write
- Cross-page tests: assert `spans_pages` is set correctly for stitched extractions


## 11) Persistent Working Memory

Claude Code sub-agents use `docs/WORKSTATE.md` as external memory for tasks modifying more than 2 files.

### Rules
- **Read first:** Before any work, read `docs/WORKSTATE.md` if it exists
- **Write often:** After every file modification or finding, update WORKSTATE.md
- **Trust the file:** After compaction, WORKSTATE.md is the source of truth
- **Don't redo:** If WORKSTATE.md says a file is modified ✓, skip it

### Scripts
```
./scripts/run-with-metrics.sh 'task description'   # New task with memory + metrics
./scripts/resume.sh                                  # Continue interrupted task
./scripts/clean.sh                                   # Archive and reset
./scripts/metrics-dashboard.sh                       # View aggregate stats
```
EOF
