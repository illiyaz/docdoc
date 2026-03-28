# CLAUDE.md — Forentis AI

Single source of truth for how this codebase is built and maintained. All contributors (human and AI) must follow these rules without exception.

See [docs/PLAN.md](docs/PLAN.md) for active implementation steps (Phase 5: Steps 21-24, Phase 6-8: Steps 25-35).
See [docs/PLAN_COMPLETED.md](docs/PLAN_COMPLETED.md) for completed steps (Phases 1-4, Steps 1-20).
See [docs/SCHEMA.md](docs/SCHEMA.md) for detailed technical architecture.
See [docs/CLAUDE_HISTORY.md](docs/CLAUDE_HISTORY.md) for detailed per-step implementation notes, bugfix narratives, and sub-run details.

---

## 0) Product Goal (non-negotiable)

**Forentis AI** is an end-to-end breach notification platform: **Identify** PII/PHI/FERPA/SPI → **Resolve** (deduplicate, link to individuals) → **Notify** per regulatory protocol.

Build an **offline-capable, air-gap-safe** system that is: evidence-backed (page/offset/bbox), deterministic-first (LLM additive only), scalable to 1000+ page PDFs (page-streaming), checkpointable (resume from last page), safe-by-default (STRICT storage, no raw PII logged), governance-ready (auditable), air-gap deployable, protocol-driven, notification-complete, access-controlled (Phase 6+), source-verifiable (Phase 6+), deadline-aware (Phase 6+).

---

## 1) Architecture: Deterministic Pipeline (NOT Agent-Based)

A **Prefect DAG pipeline** of well-defined processing stages. No autonomous agents, no LLM orchestration frameworks, no cloud API dependencies.

| Stage | File | Purpose |
|---|---|---|
| Discovery | `tasks/discovery.py` | Filesystem/DB traversal, document cataloging |
| Structure Analysis | `tasks/structure_analysis.py` | Doc type, section detection, entity role attribution |
| Document Understanding | `structure/llm_document_understanding.py` | LLM semantic schema. Fallback: heuristic + deny-lists |
| PII Detection | `tasks/detection.py` | Presidio + spaCy NER, confidence scoring |
| PII Extraction | `tasks/extraction.py` | Pattern match + context window extraction |
| Normalization | `tasks/normalization.py` | Phone/address/name/email normalization |
| RRA | `tasks/rra.py` | Entity resolution, dedup, NotificationSubject building |
| QA | `tasks/qa.py` | Validation rules, completeness checks |
| Notification | `tasks/notification.py` | List building, email delivery, print rendering |
| Error Handling | `tasks/error_handler.py` | Retry logic, failure categorization |

Each task is a plain Python class. LLM-backed reasoning gated behind `llm_assist_enabled: false`.

---

## 2) Technology Stack (Locked — No Substitutions Without Explicit Approval)

**Pipeline:** Prefect (self-hosted), PyMuPDF (fitz), Tesseract + PaddleOCR, python-docx, Ollama (qwen2.5vl:32b primary, llama3.2-vision fallback)

**Multi-format:** openpyxl, xlrd, pyxlsb, python-docx, antiword/libreoffice, extract-msg, pillow-heif+Pillow, pytesseract, dbfread, mdb-tools, sqlite3, readpst, py7zr

**PII/NLP:** Microsoft Presidio, spaCy, Regex (re), MLflow (self-hosted)

**Infrastructure:** PostgreSQL, Redis, MinIO, RabbitMQ, HashiCorp Vault, Prometheus + Grafana (all self-hosted)

**Application:** FastAPI, React + Tailwind + ShadCN, SQLAlchemy, Alembic, psycopg2, pymongo

**Air-gap rule:** Every library/model resolvable from local artifact registry. No runtime network calls. Telemetry disabled.

---

## 3) Project Structure (Canonical)

```
project-root/
├── CLAUDE.md
├── docs/                          # PLAN.md, PLAN_COMPLETED.md, SCHEMA.md
├── config/
│   ├── config.yaml                # environment config, no secrets
│   └── protocols/                 # 8 built-in YAML protocol files
├── app/
│   ├── tasks/                     # Pipeline stages (Prefect tasks)
│   ├── pipeline/                  # dag.py, two_phase.py, content_onset.py, instance_detector.py,
│   │                              # auto_approve.py, coordinate_extractor.py, reconciliation.py,
│   │                              # vision_router.py, field_map_builder.py, extraction_verifier.py,
│   │                              # record_mapper.py
│   ├── pdf/                       # reader.py, renderer.py, ocr.py, classifier.py, onset.py, stitcher.py
│   ├── pii/                       # presidio_engine.py, spacy_classifier.py, layer1_patterns.py,
│   │                              # layer2_context.py, layer3_positional.py, context_deny_list.py,
│   │                              # schema_filter.py, pattern_validator.py
│   ├── normalization/             # phone, email, name, address normalizers
│   ├── rra/                       # entity_resolver, deduplicator, fuzzy matching
│   ├── protocols/                 # Protocol dataclass, loader, registry
│   ├── notification/              # list_builder, email_sender, print_renderer, templates
│   ├── audit/                     # events, audit_log
│   ├── review/                    # roles, queue_manager, workflow, sampling
│   ├── structure/                 # models.py, heuristics.py, protocol_relevance.py, masking.py,
│   │                              # llm_analyzer.py, entity_groups.py, llm_entity_analyzer.py,
│   │                              # document_schema.py, llm_document_understanding.py,
│   │                              # llm_template_extractor.py, vision_extractor.py
│   ├── llm/                       # client.py (OllamaClient), prompts.py, audit.py
│   ├── core/                      # constants.py, policies.py, security.py, logging.py, settings.py
│   ├── db/                        # models.py (19 tables), repositories.py
│   └── api/                       # main.py, middleware/, routes/
├── frontend/src/                  # api/client.ts, pages/ (Dashboard, Projects, ProjectDetail,
│                                  # QueueView, SubjectDetail, JobSubmit, Diagnostic), components/, App.tsx
├── alembic/versions/              # 0001–0012
├── tests/                         # test_schema, test_repositories, test_policies, test_extraction,
│                                  # test_safety, test_api, test_two_phase, + many more
├── models/                        # pre-packaged spaCy and Presidio models
└── scripts/                       # retrain.py, run-with-metrics.sh, resume.sh, clean.sh
```

---

## 4) Schema Contract

Canonical DB schema (19 tables) defined by `app/db/models.py` + `alembic/versions/0001–0012` + `tests/test_schema.py`.

- Do NOT introduce new tables/columns without updating models.py, migration, test_schema.py, and repository tests simultaneously
- Migration/model mismatch must fail tests — never suppress
- All new `project_id` FKs are **nullable** for backward compatibility

---

## 5) Storage Policy & Security

**STRICT (default):** No `raw_value` anywhere. `hashed_value` required. `raw_value_encrypted` = NULL.
**INVESTIGATION:** `raw_value_encrypted` allowed (Fernet). `retention_until` required. Missing key = fail closed.

**Security (`app/core/security.py`):** SHA256(tenant_salt + raw_value) hashing. Fernet encryption (pluggable `EncryptionProvider`). **No raw PII in logs, exceptions, stack traces, or debug output — ever.**

---

## 6) Key Architectural Decisions (Locked)

Detailed in [docs/SCHEMA.md](docs/SCHEMA.md). Summary:

- **PDF:** PyMuPDF page-streaming + PaddleOCR. PII-verified onset (heuristic → Presidio). Cross-page stitching. Per-page checkpointing.
- **PII detection:** 3 layers (pattern → context → positional). 85+ patterns, 8 data categories. Protocol-driven recognizer filtering. Context deny-lists. DocumentSchema post-filter (LLM).
- **LLM Document Understanding:** One LLM call per doc → DocumentSchema (field map, people, dates, tables, suppression hints). Post-filter on Presidio, never modifies engine. Reduces FP from ~85% to ~10-15%.
- **Structure Analysis:** Heuristic-first (9 doc types, 13 sections, 5 roles). LLM additive. Cross-role merge prevention.
- **Extraction paths (priority order):** Path 0 coordinate (fixed-layout, 30-45ms/page) → Path 1 vision → Path 2a LLM table → Path 2b LLM template → Path 3 Presidio fallback.
- **Vision routing:** VisionRouter reads ONE page → determines structure type + extraction path. FieldMapBuilder bridges to coordinates. ExtractionVerifier validates completeness. **Step 26: spatial text fast-path** — text PDFs (word_count > 50) try LiteParse spatial text → text LLM first (11-28s vs 60+s vision). Falls back to vision if LiteParse unavailable or returns no fields.
- **Coordinate extraction:** LLM analyzes layout once → field map (anchors + spatial relationships) → Python extracts all pages via PyMuPDF bounding boxes. Rotation-aware (0/90/180/270). Failed pages → LLM reconciliation.
- **Analyze phase order:** discovery → cataloging → verified_onset → document_understanding → vision_routing → sample_extraction → entity_analysis → auto_approve
- **RRA:** Union-Find. Thresholds: ≥0.80 auto-accept, 0.60-0.79 review, <0.60 separate. Cross-instance merge prevention (page_range key).
- **Protocols:** 8 built-in (HIPAA, GDPR, CCPA, HITECH, FERPA, state_breach_generic, BIPA, DPDPA). YAML-configurable.
- **HITL:** 4 roles (REVIEWER, LEGAL_REVIEWER, APPROVER, QC_SAMPLER). State: AI_PENDING → HUMAN_REVIEW → LEGAL_REVIEW → APPROVED → NOTIFIED.
- **Notification:** SMTP email + WeasyPrint postal. Template-driven. Delivery gated on APPROVED.
- **Background extraction:** Daemon thread, per-doc commit, heartbeat, resume, cancellation. SSE relay with auto-reconnect.
- **Performance guards (Step 24e):** Onset-aware field map validation, deferred gap-fill (50-call budget), LLM batch cap (100, learn-then-extract hybrid), VisionRouter no-model guard.
- **Future phases:** Auth (JWT, Phase 6), RBAC (4 roles, Phase 6), access logging (Phase 6), document viewer (Phase 6), deadline tracking (Phase 6), evidence export (Phase 7), notification preview (Phase 7), re-extraction (Phase 7), manual merge/split (Phase 7).

---

## 7) Ground Rules for Code Changes

- Schema changes: update models.py + migration + test_schema.py + repository tests simultaneously
- Small, reviewable diffs — one behavior change per prompt
- Tests in same change for every behavior modification
- Never broaden scope beyond current prompt
- Run before marking done: `python -m py_compile <files> && pytest tests/test_schema.py tests/test_repositories.py tests/test_safety.py`
- Summarize: files modified + what tests verify

---

## 8) What NOT to Do

- No LangGraph, CrewAI, AutoGen, or agent frameworks
- No cloud LLM APIs (OpenAI, Anthropic, Cohere)
- No Tesseract (use PaddleOCR), no pdfplumber/PyPDF2 (use PyMuPDF)
- No loading entire PDFs into memory
- No fixed coordinates as SOLE extraction (use anchor-relative field maps)
- No storing/logging raw PII
- No making LLM mandatory for correctness
- No runtime network dependencies
- No scope creep beyond active phase

---

## 9) Current Progress

### Phases 1-4: COMPLETE (demo-ready)

Phase 1 (Deterministic Core), Phase 2 (Normalization + RRA), Phase 3 (Protocols + Notification), Phase 4 (HITL + Audit Trail).

### Phase 5 — Forentis AI Evolution: IN PROGRESS

| Step | Status | Summary |
|---|---|---|
| 1-10 | COMPLETE | Schema, Projects API, Cataloger, Density, Dedup anchors, CSV export, LLM integration, Frontend, Job Workflow, Protocol Form, Catalog Tab |
| 11 | COMPLETE | Document Structure Analysis — 9 doc types, 13 sections, 5 roles, protocol relevance, migration 0006 |
| 12 | COMPLETE | Two-Phase Pipeline — analyze → review → extract, content onset, auto-approve, migration 0007 |
| 13 | COMPLETE | LLM Entity Relationship Analysis — PII-verified onset, entity groups, migration 0008 |
| 14 | COMPLETE | LLM Document Understanding — context deny-lists, DocumentSchema + SchemaFilter, detection tuning |
| 15 | COMPLETE | Field-Level Review + Protocol Mapping — detection_review_decisions table, migration 0009 |
| 16 | COMPLETE | UX Consolidation — Dashboard, Jobs tab, Sidebar, Density, record_mapper fix |
| 17 | COMPLETE | Cross-Page Templates — DocumentTemplate, multi-page LLM, composite records, FP cleanup, auto-export |
| 18 | COMPLETE | Auditor CSV Export — schema-driven (auditor/minimal/full), +5 lineage columns, migration 0010 |
| 19 | COMPLETE | LLM Template Extraction — LLMTemplateExtractor, 3-path exclusive extraction, marker boundaries, preview, defensive parsing |
| 20 | COMPLETE | Vision-First Extraction — VisionDocumentExtractor, 4 strategies, pattern validation, batch reliability, background extraction (SSE), configurable dedup |
| 21 | COMPLETE | Coordinate Extraction — field maps, CoordinateExtractor (rotation-aware), reconciliation, schema persistence, field map validation, frontend editor |
| 22 | COMPLETE | Vision Routing — VisionRouter, FieldMapBuilder, ExtractionVerifier, pipeline wiring, frontend auditor panel |
| 23 | COMPLETE | Hybrid Pipeline — multi-format orchestration, name regex learning, archive extraction. 34 docs, 78K records, 33/34 success |
| 24e | COMPLETE | Extraction Performance — onset-aware validation, deferred gap-fill, LLM budget cap, VisionRouter guard |
| 26 | COMPLETE | LiteParse Spatial Text Routing — text PDFs route via spatial text + text LLM (11-28s vs 60+s vision), graceful fallback |

**Key metrics (March 2026):** 78,471 PII records, 34 docs (PDF/XLSX/XLS/MSG/HEIC/JPG), 30-45ms/page coordinate speed, dual-model fallback.

**~2800 tests passing.**

**Standalone scripts (proven, awaiting integration):** `scripts/test_hybrid_pipeline.py` (PDF hybrid), `scripts/forentis_extract.py` (47 extensions).

---

## 10) Testing Expectations

- Unit tests + minimal integration tests using SQLite in-memory
- `tests/test_safety.py` runs on every invocation — not optional
- Validate: behavior, schema, safety (no raw PII), extraction accuracy
- Avoid snapshot tests — assert explicit conditions
- STRICT: assert `raw_value_encrypted IS NULL`
- INVESTIGATION: assert `retention_until IS NOT NULL`

---

## 11) Persistent Working Memory

Sub-agents use `docs/WORKSTATE.md` as external memory for tasks modifying >2 files.

- **Read first**, **write often**, **trust the file**, **don't redo** completed items
- Scripts: `./scripts/run-with-metrics.sh`, `./scripts/resume.sh`, `./scripts/clean.sh`, `./scripts/metrics-dashboard.sh`
