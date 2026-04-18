# CLAUDE.md — Forentis AI

Single source of truth for how this codebase is built and maintained. All contributors (human and AI) must follow these rules without exception.

See [docs/PLAN.md](docs/PLAN.md) for active implementation steps (Phase 5: Steps 30d-30e, Phase 6-8: Steps 25-35).
See [docs/PLAN_COMPLETED.md](docs/PLAN_COMPLETED.md) for completed steps (Phases 1-4, Steps 1-20).
See [docs/SCHEMA.md](docs/SCHEMA.md) for detailed technical architecture.
See [docs/CLAUDE_HISTORY.md](docs/CLAUDE_HISTORY.md) for detailed per-step implementation notes, bugfix narratives, and sub-run details.
See [docs/DOCUMENT_TAXONOMY.md](docs/DOCUMENT_TAXONOMY.md) for the 23-category document type classification (A-Z) covering all breach notification document patterns.

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

**Pipeline:** Prefect (self-hosted), PyMuPDF (fitz), docTR (Apache 2.0, word-level bbox OCR), PaddleOCR (fallback), python-docx, Ollama (qwen2.5vl:32b primary, llama3.2-vision fallback)

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
│   └── api/                       # main.py, middleware/, routes/ (incl. intelligence.py)
├── frontend/src/                  # api/client.ts, pages/ (Dashboard, Projects, ProjectDetail,
│                                  # QueueView, SubjectDetail, JobSubmit, Diagnostic,
│                                  # IntelligenceTab), components/, App.tsx
├── alembic/versions/              # 0001–0013
├── tests/                         # test_schema, test_repositories, test_policies, test_extraction,
│                                  # test_safety, test_api, test_two_phase, + many more
├── models/                        # pre-packaged spaCy and Presidio models
└── scripts/                       # retrain.py, run-with-metrics.sh, resume.sh, clean.sh
```

---

## 4) Schema Contract

Canonical DB schema (19 tables) defined by `app/db/models.py` + `alembic/versions/0001–0013` + `tests/test_schema.py`.

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

- **PDF:** PyMuPDF page-streaming + docTR (primary OCR, Apache 2.0, MPS/CUDA) + PaddleOCR (fallback). PII-verified onset (heuristic → Presidio). Cross-page stitching. Per-page checkpointing.
- **PII detection:** 3 layers (pattern → context → positional). 85+ patterns, 8 data categories. Protocol-driven recognizer filtering. Context deny-lists. DocumentSchema post-filter (LLM).
- **LLM Document Understanding:** One LLM call per doc → DocumentSchema (field map, people, dates, tables, suppression hints). Post-filter on Presidio, never modifies engine. Reduces FP from ~85% to ~10-15%.
- **Structure Analysis:** Heuristic-first (9 doc types, 13 sections, 5 roles). LLM additive. Cross-role merge prevention.
- **Extraction paths (Step 37 strategy, April 2026):** Auto-selected based on marker detection: **Strategy A** (marker-filter: Python filters to ~5 lines per page using context markers, 32b extracts from snippets, 95-100% accuracy) → **Strategy B** (full text batch: 3 pages per 32b call with entity_role prompt, 92-98% accuracy) → **Strategy C** (vision: 90B on rendered images, scanned-only). Legacy paths (coordinate, LLM table/template, Presidio) remain behind `USE_TEXT_LLM_BATCH=false` flag.
- **Vision routing:** VisionRouter reads ONE page → determines structure type + extraction path. FieldMapBuilder bridges to coordinates. ExtractionVerifier validates completeness. **Step 26: spatial text fast-path** — text PDFs (word_count > 50) try LiteParse spatial text → text LLM first (11-28s vs 60+s vision). Falls back to vision if LiteParse unavailable or returns no fields.
- **Coordinate extraction:** LLM analyzes layout once → field map (anchors + spatial relationships) → Python extracts all pages via PyMuPDF bounding boxes. Rotation-aware (0/90/180/270). Failed pages → LLM reconciliation.
- **Analyze phase order:** discovery → document creation → **segregation (Stage 1.7, LLM classification)** → cataloging → verified_onset → document_understanding → vision_routing → sample_extraction → entity_analysis → auto_approve
- **Extract phase order:** per-doc extraction (5-path cascade) → retry gap-fill → ExtractionVerifier vision gap-fill → **GapDetector + GapFiller (4-path cascade, persisted to JSON)** → entity resolution → deduplication → notification subjects
- **RRA:** Union-Find. Thresholds: ≥0.80 auto-accept, 0.60-0.79 review, <0.60 separate. Cross-instance merge prevention (page_range key).
- **Protocols:** 8 built-in (HIPAA, GDPR, CCPA, HITECH, FERPA, state_breach_generic, BIPA, DPDPA). YAML-configurable.
- **HITL:** 4 roles (REVIEWER, LEGAL_REVIEWER, APPROVER, QC_SAMPLER). State: AI_PENDING → HUMAN_REVIEW → LEGAL_REVIEW → APPROVED → NOTIFIED.
- **Notification:** SMTP email + WeasyPrint postal. Template-driven. Delivery gated on APPROVED.
- **Background extraction:** Daemon thread, per-doc commit, heartbeat, resume, cancellation. SSE relay with auto-reconnect.
- **Performance guards (Step 24e):** Onset-aware field map validation, deferred gap-fill (50-call budget), LLM batch cap (100, learn-then-extract hybrid), VisionRouter no-model guard.
- **Scale hardening (3000+ pages, April 2026):** Adaptive per-doc timeout (`_compute_doc_timeout()`: 30min base + 2s/page beyond 200, cap 4hr — 3000pg → 123min). LLM gap-fill budget scales with gap count (`min(500, gaps × 0.6)` — was capped at 200). Circuit breaker on LLM retries (5 consecutive failures → abort early, don't waste time on dead model; any success resets counter). Failed pages tracked and surfaced as gaps. Progress DB commits reduced to every 10 batches (was every batch). Page text dict cleared after extraction. All PyMuPDF `_forget_page()` calls verified across pipeline (gap_filler had 4 missing). Pre-onset pages skipped when building text dict.
- **Intelligence tab (Step 30a):** Read-only diagnostic view after analysis — LLM understanding, routing decisions, field maps, entity analysis, sample extractions. Test-extract (Tier 1): extract N pages from onset without persisting. Correction memory: user corrections stored in metadata_json + JSONL for future few-shot prompt injection.
- **LLM prompt coverage:** UNDERSTAND_DOCUMENT and UNDERSTAND_MULTI_PAGE_DOCUMENT explicitly handle educational docs (FERPA), HR/payroll docs. Field maps with only PERSON+LOCATION are valid. Schema persisted per-doc (not batched) to survive per-doc failures.
- **OCR tool evaluation (April 2026):** Tested docTR, Surya, Marker, MinerU across 44 files (23 categories). docTR selected as primary OCR: Apache 2.0, 16x faster than Surya, word-level bboxes, best data completeness on tabular docs. Scale tests: docTR 37-767x faster than Surya on 225-page docs. Multi-page completeness: 500 pages, zero empty pages, 98.8% SSN coverage. Surya abandoned for extraction (catastrophic perf degradation under load). MinerU abandoned (AGPL + model arch mismatch). **docTR integrated into pipeline** as `DocTREngine` in `app/readers/ocr.py` with MPS (Apple Silicon) auto-detection, PaddleOCR as automatic fallback.
- **LLM model selection (April 2026):** Tested 6 models across 12 files. **qwen2.5:32b selected as primary text model** for accuracy (97.3% on school reports vs 87% for 7b, 92% for 14b). Honest about missing labels (14b hallucinated markers). Config: `OLLAMA_MODEL=qwen2.5:32b` (text + extraction), `OLLAMA_UNDERSTANDING_MODEL=qwen2.5:32b`, `OLLAMA_VISION_MODEL=llama3.2-vision:90b` (vision-only for scanned/image docs). On Mac M4 slower (~18-25min/225pg) but on A100 GPU ~2-3min. Segregation routes text PDFs to text model.
- **Field map auto-correction:** `auto_correct_field_map()` in `app/pipeline/field_map_builder.py` corrects LLM-guessed spatial relationships using actual PyMuPDF word positions. Handles blank gaps between anchor and data section. LLM says "line_below" → code corrects to "lines_below_6" based on real layout.
- **Document structure classification (Categories A-D):** LLM prompts explicitly guide classification of one-per-page labeled (A), delimited blocks (B), true tables (C), and multi-person positional (D) documents. entity_role on field maps filters institutional/provider names (teachers, doctors) from extraction.
- **LLM-first segregation (Step 30e):** File classification (PII vs non-PII) uses vision LLM on page 1-2, NOT OCR+regex. One LLM call returns: PII yes/no, document type, field inventory, role attribution (primary subject vs secondary contacts), **country_hint (ISO 3166-1 alpha-2, used by gov-ID classifier downstream)**. ~2-3s/file. Two modes: folder (bulk segregation → grouping → auditor review) and single-file (inline segregation → direct analysis). **Late-onset sampling:** for docs >20 pages, mid-page (50%) is additionally sampled if pages 1-2 showed no PII. For docs >50 pages, 3/4-page (75%) is also sampled. Catches tax-return K-1 packets and pension-plan member sections where cover/TOC hides deeper PII. *Follow-up planned:* replace 50/75% point-sampling with stratified N-sample (25/50/75%, configurable N) for robustness against skewed distributions.
- **Geo-neutral government-ID classifier (`app/pii/gov_id_classifier.py`):** Maps raw ID values to canonical types (UK_NINO, IN_PAN, BR_CPF, IT_CF, SG_NRIC, CN_RESID, etc.) across 40+ formats in ~35 countries. Strict alphanumeric formats self-identify; digit-only formats use segregation's country_hint to disambiguate (US_SSN vs NL_BSN vs IL_ID all are 9 digits). Wired at `deduplicator._build_subject` — runs on raw_government_id before falling back to entity_types_found, so `notification_subjects.government_id_type` reflects the true jurisdiction rather than a hardcoded "US_SSN" label.
- **Segregation Review UI (Step 30e):** New screen between ingestion and analysis. Card layout per document group: type badge, file count, field inventory chips, thumbnail previews, approve/reject per group or bulk. Non-PII group shown separately with "Rescue" action. Corrections feed into JSONL for future few-shot prompt injection.
- **LLM judge (Phase 8):** When multiple OCR engines available, LLM compares outputs per page and selects best result. Integrated into first-look LLM call. Activates only when quality is uncertain. Deferred — docTR alone achieves 98.8% coverage.
- **Role attribution (FIXED):** `entity_role` flows end-to-end: segregation role_map → metadata_json → VisionRouter pii_fields → FieldMapBuilder → FieldMapping.entity_role → CoordinateExtractor → DetectionResult → record_mapper → PIIRecord.entity_role → entity_resolver cross-role merge prevention. Schema-skip path also enriched.
- **Automated gap detection + fill (Step 30e-6):** After extraction, detect page-level gaps (missing pages on repeating templates), field-level gaps (expected fields not extracted), truncation (incomplete records). Auto-fill aggressively through fallback extraction paths (coordinate → LLM template → vision → Presidio, max 3 LLM calls per gap). Only genuinely unrecoverable gaps reach the auditor.
- **Adaptive extraction intelligence (Step 30h):** Four self-tuning features: (1) Strategy A field-aware prompts — marker-filter dynamically includes SSN/DOB/phone/email/account fields based on segregation field_inventory. (2) Post-batch quality gate — after first batch, LLM diagnoses missing fields and adjusts extraction hints for remaining batches (1 call/doc). (3) Adaptive onset — non-PII docs with 50+ pages sampled at 25%/50%/75% for late-onset PII (catches K-1 schedules starting at page 52). (4) Self-correcting extraction loop — if gap fill rate <30%, LLM diagnoses sample unfilled pages ("what PII is here and in what format?"), re-extracts all unfilled pages with diagnosis as prompt context. Total overhead: 2-4 extra LLM calls per doc.
- **Segregation regex fallback (Step 30h):** When Ollama is unavailable, segregation falls back to deterministic PII pattern scan (9 patterns: SSN, gov ID, DOB, phone, email, DL, person names, addresses, financial). 2+ matches → PII with 0.65-0.85 confidence. Prevents silent misclassification of breach docs as non-PII.
- **Late-onset PII (Step 30h):** Adaptive onset sampling at 25%/50%/75% of doc catches late-onset PII (e.g., K-1 schedules at page 52 in 100-page tax return). Late-onset page plumbed into `content_onset_page` — extraction skips boilerplate pages. AWIR-993: 99 pages → 27 pages extracted (73% fewer LLM calls).
- **Vision fallback for failed text pages (Step 30h):** Text-first, vision-fallback per page range. After Strategy B text extraction, if zero-record pages exist where PII was expected (from segregation), render those pages as images and send to 90B vision model. Only genuinely hard pages (OCR-degraded forms, complex tabular layouts) hit the slow vision model. Implemented in gap_filler.py `_try_vision_fallback()`. Benchmark: AWIR-993 K-1 schedules, AWIR-482 pension plan headers.
- **LLM record validation (Step 30h):** After extraction, before gap detection, sends one LLM call per doc with all extracted records + document type. LLM scores each record as VALID or GARBAGE (form codes, legal entities, empty names, parsing artifacts). Purged records cause their pages to appear as gaps, naturally triggering gap detection → self-correct → vision fallback. Zero hardcoding, multi-geo ready — the LLM understands context (IRS K-1 form codes, UK NI numbers, Indian PAN, etc.). Cost: 1 call/doc (~5s). Implemented in `app/pipeline/record_validator.py`, wired as Stage 1.4 in two_phase.py.
- **Completeness-driven vision recovery (Step 30h):** After record validation, counts **actionable** subjects (name + gov ID, not just names). If <50% of expected, gets name roster from summary pages (1 LLM call, JSON + plain text fallback), then vision-extracts pages at **300 DPI** with geo-neutral prompt ("find any person's unique government-issued identification number"). Gov ID parser accepts any format (SSN, TIN, NI, PAN, etc.). Self-correct sampling picks beginning/middle/end of gap list to avoid boilerplate bias. AWIR-993 result: 0 SSNs → 3 SSNs recovered from K-1 forms via vision. Cost: 1 roster + N vision calls (max 15 pages). Implemented in `app/pipeline/completeness_checker.py`.
- **DB-level dedup (Step 30h):** `_find_existing` in `deduplicator.py` now matches by canonical_name + gov_id_type + project_id (was email/phone only). `_merge_into` combines page ranges and fills missing fields. Post-reconciliation SQL dedup planned: `DELETE ... USING` on same canonical_name + project_id.
- **Data purge API (Step 30h):** `DELETE /api/jobs/{id}/purge` — removes extractions, notification_subjects, gap files, clears extracted_records from metadata. Prevents DB bloat from repeated test runs. Keeps ingestion_run audit trail.
- **Extraction QA screen (Step 30e-7):** Post-extraction auditor confidence screen. Summary dashboard (completeness stats, visual progress bar). Smart sample panel (curated not random: largest group, gap-filled, merged, edge cases). Gaps panel with page text snippets (show actual content for auditor verification), bulk resolution (Dismiss Low/Medium/All), per-gap resolve/N-A/unrecoverable. Approval gated on resolving all high-severity gaps. **Known bug:** post-approval notification tab experience incomplete (deferred to Step 29b).
- **Future phases:** Auth (JWT, Phase 6), RBAC (4 roles, Phase 6), access logging (Phase 6), deadline tracking (Phase 6), evidence export (Phase 7), re-extraction (Phase 7), manual merge/split (Phase 7), LLM fine-tuning from correction memory (Phase 8+).

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
- No Tesseract (use docTR or PaddleOCR), no pdfplumber/PyPDF2 (use PyMuPDF)
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
| 26b | COMPLETE | Source Document Viewer — on-demand page rendering with PII-type colour-coded bbox overlays, 3 API endpoints, DocumentViewer component |
| 26c | COMPLETE | Merge Explanation — build_confidence_explained() with per-anchor signals, migration 0013, MergeExplanation component |
| 26d | COMPLETE | Auditor Workflow Polish — analysis filter tabs, dedup summary, extraction progress bar, plain-English config, export filtering, delivery dashboard |
| 29a | COMPLETE | Notification Preview — email/letter preview with masked PII, NotificationPreview component |
| 30a | COMPLETE | Intelligence Tab — document understanding diagnostic view, test-extract (Tier 1), correction memory for LLM few-shot learning |
| 30b | COMPLETE | Extraction Quality — phone validation across all 5 paths, DL regex tightened, name quality gate relaxed, schema persistence per-doc |
| 30c | COMPLETE | LLM Prompt Coverage — school/HR/educational docs recognized (FERPA), PERSON+LOCATION-only field maps valid |
| 30d | COMPLETE | OCR Tool Evaluation — docTR selected: Apache 2.0, 16x-767x faster than Surya, word-level bbox, 98.8% SSN coverage on 500 pages. Surya abandoned (catastrophic perf degradation). MinerU abandoned (AGPL + model mismatch). Scale tests + multi-page completeness verified. |
| 30e | COMPLETE | LLM-First Segregation + Review UI — vision LLM file classification, document grouping, Segregation Review screen (card layout, approve/reject), role attribution plumbing fix, correction memory. Two modes: folder (bulk) and single-file (direct). Automated gap detection + fill (fallback extraction paths). Extraction QA screen (smart sampling, manual gap resolution, approval gating). **All 7 sub-steps done + pipeline wiring verified.** 135 tests total for 30e. |
| 30f | COMPLETE | **Performance + Quality Sprint (April 11-12, 2026):** docTR integrated as primary OCR (0.55s/page on MPS, PaddleOCR fallback). qwen2.5:7b selected as primary text LLM (9x faster than 90B, fewer critical drifts across 12 test files). Segregation text-first routing (8.8s vs 150s for text PDFs). Structure analysis LLM skip when segregation confident. Tabular detection trusts LLM rpp over name count (SSNs override). entity_role on field maps (prompt + parser + segregation enrichment). Field map auto-correction via PyMuPDF word positions. 4 extraction bugs fixed (selector bypass, method signatures, gap analysis attribute). Race condition in job creation fixed. End-to-end UI flow wired (auto-navigate to QA, prominent Review button, post-QA navigation). LLM judgment test script (`scripts/test_llm_judgment.py`). Category B/C/D document structure guidance in prompts. Analysis time: **19.7min → 3.75min (81% faster)**. |
| 37 | COMPLETE | **Text LLM Batch Extraction (April 12-13, 2026):** Strategy A (marker-filter: Python filters to ~5 lines, 32b extracts snippets, 95-100% accuracy on labeled docs), Strategy B (full text batch: 3 pages per 32b call with entity_role prompt, 92-98% on label-less docs), Strategy C (vision: 90B for scanned/image). Auto-selected via `repeating_unit_detector.py` marker detection. Model upgraded to qwen2.5:32b for all text calls. Cross-validation, hallucination check, address validation, frequency filter. QA screen revamped (bulk gap resolution, page text snippets, visual progress bar). |
| 37b | COMPLETE | **Repeating Unit Detection (April 13, 2026):** 9-page sampling (3 onset + 3 mid + 3 end). Returns record_unit (page/block/row/multi_page), separator, records_per_page. Multi-subject prompt variant for Category B/C (payroll registers, tabular docs). Overlapping page windows for multi_page record_unit. Vision fallback for invisible separators (horizontal rules, shaded rows) when page >4000 chars. |
| 30g | COMPLETE | **Scale Hardening (April 13-14, 2026):** Adaptive per-doc timeout (30min base + 2s/page, 3000pg → 123min). LLM gap-fill budget scales to 500 calls. Circuit breaker on LLM retries (5 consecutive failures → abort, success resets). PDF memory: `_forget_page()` across all pipeline files, pre-onset skip, text dict cleanup. Progress commits batched (every 10). 3 pre-existing test failures fixed. |
| 30h | COMPLETE | **Adaptive Extraction Intelligence (April 15-17, 2026):** Full self-tuning extraction chain. (1) Strategy A field-aware prompts — marker-filter includes SSN/DOB/phone/email from field_inventory. (2) Post-batch quality gate — LLM diagnoses missing fields, adjusts hints. (3) Adaptive onset — 50+ page docs sampled at 25%/50%/75% for late-onset PII. (4) Self-correcting loop — diagnoses + re-extracts failed pages. (5) LLM record validation — purges ~45% garbage (form codes, legal entities). (6) Completeness-driven vision recovery — counts actionable subjects (name+gov_id), if <50% complete, gets name roster from summary pages, vision-scans at 300 DPI with geo-neutral prompt. (7) DB-level dedup — `_find_existing` matches name+gov_id+project. (8) Data purge API — DELETE /api/jobs/{id}/purge. Segregation regex fallback (9 patterns). Segregation UI banner. Gap detector prevalence threshold (55% fewer false gaps). AWIR-993 benchmark: 0 SSNs → 3 SSNs via vision recovery. Complex1: 0 SSNs → 50 SSNs via field-aware prompts. |

**Key metrics (April 2026):** 78,471 PII records, 34 docs (PDF/XLSX/XLS/MSG/HEIC/JPG), 30-45ms/page coordinate speed, dual-model (qwen2.5:32b text + llama3.2-vision:90b vision). Phone validity 99.5% (was 85.9%). Gov ID coverage 95% (was 85.2%). Analysis time 3.75min (was 19.7min). Extraction accuracy 97.3% (32b on school reports). Scale: tested to 500 pages, designed for 3000+. Overnight 12-doc run (April 15): 2,105 records, 1,157 subjects. Adaptive intelligence (April 15-17): Complex1 SSNs 0→50 (field-aware prompts), AWIR-993 SSNs 0→3 (300 DPI vision recovery from K-1 forms), record validation purges ~45% garbage, gap detector 55% fewer false positives.

**~2850 tests passing.** (19 tables, migration 0013)

**Standalone scripts (proven, awaiting integration):** `scripts/test_hybrid_pipeline.py` (PDF hybrid), `scripts/forentis_extract.py` (47 extensions), `scripts/test_llm_judgment.py` (LLM model comparison).

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
