# Completed Steps — Forentis AI

For active steps, see [PLAN.md](PLAN.md). For project overview, see [../CLAUDE.md](../CLAUDE.md).
For detailed per-step implementation notes, see [CLAUDE_HISTORY.md](CLAUDE_HISTORY.md).

---

## Phase 1 — Deterministic Core: COMPLETE
Schema, migrations 0001-0004, repositories, policies, security, logging, settings, readers (PDF/Excel/DOCX/CSV/HTML/Email/Parquet), PII detection (3 layers, 85+ patterns), discovery task, pipeline skeleton.

## Phase 2 — Normalization + RRA: COMPLETE
Phone/email/name/address normalizers, entity resolver (Union-Find, confidence ladder), deduplicator, fuzzy matching.

## Phase 3 — Protocol Configuration + Notification Delivery: COMPLETE
8 protocol YAML files, Protocol dataclass/loader/registry, notification list builder, SMTP email sender (retry + rate limit), WeasyPrint print renderer, 4 HTML templates.

## Phase 4 — Enhanced HITL + Comprehensive Audit Trail: COMPLETE
4 roles (REVIEWER, LEGAL_REVIEWER, APPROVER, QC_SAMPLER), 4 review queues, WorkflowEngine state machine, SamplingStrategy, audit events (append-only).

## Phase 5 — Forentis AI Evolution: IN PROGRESS

| Step | Summary |
|---|---|
| 1-10 | Schema (migration 0005), Projects API, Cataloger, Density, Dedup anchors, CSV export, LLM integration, Frontend, Job Workflow, Protocol Form, Catalog Tab |
| 11 | Document Structure Analysis — 9 doc types, 13 sections, 5 roles, migration 0006 |
| 12 | Two-Phase Pipeline — analyze → review → extract, content onset, auto-approve, migration 0007 |
| 13 | LLM Entity Relationship Analysis — PII-verified onset, entity groups, migration 0008 |
| 14 | LLM Document Understanding — context deny-lists, DocumentSchema + SchemaFilter, detection tuning |
| 15 | Field-Level Review + Protocol Mapping — detection_review_decisions, migration 0009 |
| 16 | UX Consolidation — Dashboard, Jobs tab, Sidebar, Density, record_mapper fix |
| 17 | Cross-Page Templates — DocumentTemplate, multi-page LLM, composite records, FP cleanup, auto-export |
| 18 | Auditor CSV Export — schema-driven (auditor/minimal/full), +5 lineage columns, migration 0010 |
| 19 | LLM Template Extraction — LLMTemplateExtractor, 3-path exclusive, marker boundaries, preview, defensive parsing, migrations 0011-0012 |
| 20 | Vision-First Extraction — VisionDocumentExtractor, 4 strategies, pattern validation, batch reliability, background extraction (SSE), configurable dedup |
| 21 | Coordinate Extraction — field maps, CoordinateExtractor (rotation-aware), reconciliation, schema persistence, field map validation, frontend editor |
| 22 | Vision Routing — VisionRouter, FieldMapBuilder, ExtractionVerifier, pipeline wiring, frontend auditor panel |
| 23 | Hybrid Pipeline — multi-format orchestration, name regex learning, archive extraction. 34 docs, 78K records, 33/34 success |
| 24e | Extraction Performance — onset-aware validation, deferred gap-fill, LLM budget cap, VisionRouter guard |
| 26 | LiteParse Spatial Text Routing — text PDFs route via spatial text + text LLM (11-28s vs 60+s vision) |
| 26b | Source Document Viewer — on-demand page rendering, PII-type bbox overlays, 3 API endpoints |
| 26c | Merge Explanation — build_confidence_explained(), per-anchor signals, migration 0013 |
| 26d | Auditor Workflow Polish — analysis filter tabs, dedup summary, extraction progress bar, plain-English config, export filtering, delivery dashboard |
| 29a | Notification Preview — email/letter preview with masked PII rendering |
| 30a | Intelligence Tab — document understanding diagnostic view, test-extract (Tier 1), correction memory |
| 30b | Extraction Quality — phone validation across all 5 paths, DL regex tightened, schema persistence per-doc |
| 30c | LLM Prompt Coverage — school/HR/educational docs recognized (FERPA), PERSON+LOCATION-only field maps valid |
| 30d | OCR Tool Evaluation — docTR selected (Apache 2.0, 16-767x faster, word-level bbox, 98.8% SSN coverage) |
| 30e | LLM-First Segregation + Review UI + Extraction QA — 7 sub-steps, 135 tests, full pipeline wiring |
| 30f | Performance + Quality Sprint — docTR integrated (MPS), qwen2.5:7b primary text LLM (9x faster), segregation text-first (8.8s), structure LLM skip, tabular detection fix, entity_role on field maps, field map auto-correction, 4 extraction bugs fixed, race condition fix, end-to-end UI flow, LLM judgment test script, Category B/C/D prompts. **Analysis: 19.7min → 3.75min (81% faster)** |

**Key metrics:** ~2850 tests, 19 tables, 13 migrations, 78K PII records from 34 docs. Analysis time 3.75min (was 19.7min). Primary models: qwen2.5:7b (text) + llama3.2-vision:90b (vision). OCR: docTR on MPS.
