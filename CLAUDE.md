# CLAUDE.md — Forentis AI

Single source of truth for how this codebase is built and maintained. All contributors (human and AI) must follow these rules without exception.

See [docs/PLAN.md](docs/PLAN.md) for full implementation plan and step-by-step details.
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
| PII Detection Agent | `tasks/detection.py` — Presidio + spaCy NER, confidence scoring |
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
│   ├── PLAN.md                    # full implementation plan
│   └── SCHEMA.md                  # detailed technical architecture
├── config/
│   ├── config.yaml                # all environment config, no secrets
│   └── protocols/                 # 6 built-in YAML protocol files
├── app/
│   ├── tasks/                     # pipeline stages (Prefect tasks)
│   │   ├── discovery.py
│   │   ├── detection.py
│   │   ├── extraction.py
│   │   ├── cataloger.py           # Phase 5 Step 3
│   │   ├── qa.py
│   │   └── error_handler.py
│   ├── pipeline/
│   │   └── dag.py                 # Prefect DAG wiring
│   ├── pdf/
│   │   ├── reader.py              # PyMuPDF streaming wrapper
│   │   ├── ocr.py                 # PaddleOCR integration
│   │   ├── classifier.py          # digital/scanned/corrupted detection
│   │   ├── onset.py               # content onset detection
│   │   └── stitcher.py            # cross-page tail-buffer logic
│   ├── pii/
│   │   ├── presidio_engine.py     # Presidio wrapper + custom recognizers
│   │   ├── spacy_classifier.py    # context window classification
│   │   ├── layer1_patterns.py     # regex pattern library (85+ patterns)
│   │   ├── layer2_context.py      # Layer 2 context window logic
│   │   └── layer3_positional.py   # Layer 3 header inference
│   ├── normalization/             # phone, email, name, address normalizers
│   ├── rra/                       # entity resolver, deduplicator, fuzzy matching
│   ├── protocols/                 # Protocol dataclass, loader, registry
│   ├── notification/              # list builder, email sender, print renderer, templates
│   ├── audit/                     # events, audit_log
│   ├── review/                    # roles, queue_manager, workflow, sampling
│   ├── llm/
│   │   ├── client.py              # OllamaClient — governance-gated LLM wrapper
│   │   ├── prompts.py             # Prompt templates (classify, assess, suggest)
│   │   └── audit.py               # LLM call logging (log_llm_call, get_llm_calls)
│   ├── core/
│   │   ├── constants.py           # ENTITY_CATEGORY_MAP, DATA_CATEGORIES (8 categories)
│   │   ├── policies.py            # STRICT / INVESTIGATION storage policy
│   │   ├── security.py            # hashing, encryption, EncryptionProvider
│   │   ├── logging.py             # PIISafeFilter
│   │   └── settings.py            # pydantic-settings
│   ├── db/
│   │   ├── models.py              # SQLAlchemy ORM models (17 tables)
│   │   └── repositories.py        # thin data access layer
│   └── api/
│       ├── main.py
│       ├── middleware/
│       └── routes/                # health, diagnostic, jobs, projects, protocols
├── frontend/                      # React Forentis AI UI
│   └── src/
│       ├── api/client.ts          # API client (types + functions)
│       ├── pages/
│       │   ├── Dashboard.tsx      # Review dashboard
│       │   ├── Projects.tsx       # Project list + create
│       │   ├── ProjectDetail.tsx  # Project detail (5 tabs, guided protocol form)
│       │   ├── QueueView.tsx      # Review queue
│       │   ├── SubjectDetail.tsx  # Subject detail
│       │   ├── JobSubmit.tsx      # Job submission
│       │   └── Diagnostic.tsx     # Diagnostic scan
│       ├── components/            # Shared components (ShadCN + custom)
│       └── App.tsx                # Routes + sidebar + Forentis AI branding
├── alembic/
│   └── versions/                  # 0001–0005
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
│   └── test_llm.py
├── models/                        # pre-packaged spaCy and Presidio models
└── scripts/
    └── retrain.py                 # supervised retraining from human labels
```

---

## 4) Schema Contract

The canonical DB schema (17 tables) is defined by:
- `app/db/models.py`
- `alembic/versions/0001_initial.py` through `0005_projects_and_protocols.py`
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

- **PDF processing:** PyMuPDF page-streaming + PaddleOCR for scanned pages. Dual-path (digital vs scanned). Content onset detection. Cross-page tail-buffer stitching. Checkpointing per page.
- **PII detection:** Three layers (pattern match → context window → positional header). Presidio + spaCy. 85+ patterns covering PII/PHI/FERPA/SPI/PPRA. 8 data categories (PII, SPII, PHI, PFI, PCI, NPI, FTI, CREDENTIALS) with multi-category mapping per entity type.
- **RRA:** Entity resolution via Union-Find. Confidence-weighted merge signals. Threshold: 0.80 auto-accept, 0.60–0.79 human review, <0.60 separate.
- **Protocols:** 6 built-in (HIPAA, GDPR, CCPA, HITECH, FERPA, state_breach_generic). YAML-configurable. Selected once per job.
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

### Phase 5 — Forentis AI Evolution: COMPLETE

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
| 9. Guided Protocol Form | COMPLETE | Replaced raw JSON textarea with guided form: base protocol dropdown (6 presets), entity type checkboxes (Identity/Financial/Health), confidence slider, dedup anchor multi-select, sampling config, storage policy radios, reorderable export fields, raw JSON toggle for power users |

**1400 tests passing after Steps 1–8. Phase 5 complete.**

See [docs/PLAN.md](docs/PLAN.md) for full step-by-step implementation details.

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
