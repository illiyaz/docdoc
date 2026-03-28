# CLAUDE_HISTORY.md — Detailed Implementation History

Detailed per-step, per-bugfix implementation notes extracted from CLAUDE.md. This file preserves full context for all completed Phase 5 work (Steps 1-24e) including sub-runs, bugfixes, test counts, and production findings.

For the compact active reference, see [../CLAUDE.md](../CLAUDE.md).
For active implementation steps, see [PLAN.md](PLAN.md).

---

## Phase 5 Step Table (Detailed)

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
| 12. Two-Phase Pipeline | COMPLETE | Analyze -> Review -> Extract workflow. Content onset detection (all file types), sample PII extraction from first content page, document-level analysis review (approve/reject/approve-all), auto-approve (confidence-based + protocol-configurable), Phase 2 full extraction on approved docs, migration 0007, `DocumentAnalysisReview` table (18 total), frontend pipeline mode toggle + analysis review panel, 28 new tests |
| 13. LLM Entity Relationship Analysis | COMPLETE | PII-verified onset detection (two-pass: heuristic candidates -> Presidio verification). LLM entity relationship analysis: reads onset page + PII detections, proposes entity groups with confidence + rationale. New analyze stages: `verified_onset` + `entity_analysis`. `EntityRelationshipAnalysis` dataclass, `LLMEntityAnalyzer`, `ANALYZE_ENTITY_RELATIONSHIPS` prompt. API returns entity groups/relationships/guidance. Frontend entity group cards with role badges, relationship display, extraction guidance. Migration 0008 (`documents.entity_analysis` JSON column). 20 new tests. |
| 14. LLM Document Understanding & Detection Quality | COMPLETE | Context deny-lists, tighter Presidio patterns, protocol-driven recognizer filtering, LLM Document Understanding (DocumentSchema + SchemaFilter + TableSchema), detection tuning, Catalog tab UX. |
| 15. Field-Level Review + Protocol Mapping | COMPLETE | Two-tier detection toggle, protocol field mapping, `detection_review_decisions` table (migration 0009, 19 tables). |
| 16. UX Consolidation: Dashboard, Jobs, Sidebar, Density | COMPLETE | Dashboard command center, Jobs tab (cancel/archive/filter/pagination), Sidebar 8->5 items, Density state-driven display. |
| 17. Cross-Page Template Linking + FP Cleanup + Auto-Export | COMPLETE | DocumentTemplate, PageRole, multi-page LLM reading, build_composite_record, financial term deny-list, cross-type suppression, auto-CSV-export. |
| 18. Auditor-Ready CSV Export with Lineage | COMPLETE | Schema-driven CSV (auditor/minimal/full), +5 lineage columns on NotificationSubject (migration 0010), gov ID masking, preview endpoint. |
| 19. Schema-Driven LLM Extraction for Templates | COMPLETE | LLMTemplateExtractor, ENTITY_EXTRACTION_GUIDE (17 types), ALWAYS_EXTRACT_IF_PRESENT, 3-path extraction (exclusive), cross-batch dedup, marker-based instance boundaries, 24 tests. |
| 20. Vision-First Extraction Architecture | COMPLETE | Vision-language model as primary extractor. VisionDocumentExtractor, PDF page renderer, instance boundary detector, OllamaClient.generate_with_images. 4 extraction strategies: template, table, vision page, Presidio fallback. Pattern validation. Per-protocol model config. Table extraction. Background extraction (SSE decoupling). Configurable dedup anchors. Batch reliability with retry/backoff. 79 new tests. |
| 21. Coordinate-Based Extraction for Structured Documents | COMPLETE | For fixed-layout documents (accounting statements, payslips), LLM analyzes layout once -> builds field map (anchor text + spatial relationships + coordinates) -> Python extracts ALL pages using coordinate-based text extraction in seconds. Auditor reviews/edits field map before extraction. Reconciliation: failed pages sent to LLM fallback. ADDITIVE -- existing LLM template/table/page paths unchanged. |
| 22. Vision-Based Document Routing | COMPLETE | VisionRouter reads ONE page with vision model -> determines structure type, PII fields, extraction path. FieldMapBuilder bridges vision PII to PyMuPDF coordinates. ExtractionVerifier validates post-extraction completeness. Frontend auditor panel shows vision routing results with field map editor. |
| 23. Hybrid Pipeline & Multi-Format Orchestration | COMPLETE | Gap analysis + 2 fixes. Static value filtering, name format learning, consistency scoring all in UI pipeline. Gap 1 fix: `_learn_name_regex()` in `coordinate_extractor.py`. Gap 2 fix: Archive pre-extraction in `discovery.py`. 34 real documents, 78,471 records, 33/34 working. |
| 24e. Extraction Performance Fixes | COMPLETE | 4 fixes from E2E test (34 docs, 20K pages): (1) onset-aware field map validation (2-strategy), (2) deferred post-extraction gap-fill with 50-call budget, (3) LLM batch budget cap at 100 (learn-then-extract hybrid for over-budget docs), (4) VisionRouter no-model guard. |

---

## Standalone Scripts (proven, awaiting integration)

| Script | Purpose | Formats |
|---|---|---|
| `scripts/test_hybrid_pipeline.py` | PDF-specific hybrid extraction engine | Text + scanned PDFs |
| `scripts/forentis_extract.py` | Unified orchestrator for all formats | 47 file extensions |

---

## Key Proven Metrics (March 2026, 34 real breach documents)

- 78,471 PII records extracted across PDF, XLSX, XLS, MSG, HEIC, JPG
- 33/34 files successful (1 genuinely empty)
- Coordinate-based audit: 17/17 PASS on text PDFs
- Speed: 30-45ms per page (vs 5+ seconds/page for LLM-per-page)
- Dual-model fallback: qwen2.5vl primary -> llama3.2-vision catches 500 errors
- Scanned PDFs: vision OCR reads ID cards, receipts, dental statements
- MSG emails: body text PII extraction (not just attachments)
- HEIC photos: 2x upscale OCR for phone camera images

End-to-end workflow:
```
Folder -> Discover -> Route -> Extract -> Audit -> Normalize -> Dedup -> Sample -> Review -> Notify
```

---

## Bugfixes (Production)

### Extraction preview multi-page read
Preview now reads ALL pages of instance 0 (not just identity page). `build_preview_extraction_prompt()` asks LLM for per-field page numbers (`{value, page}` format). `_parse_preview_response()` parses LLM output with canonical field mapping. Instance count uses `find_instance_boundaries()` when marker set. 11 net new tests.

### CRITICAL: Cross-instance dedup over-merging
149 unique individuals were being collapsed to 28 rows. Root cause: `_deduplicate_records()` keyed on name only, merging people from DIFFERENT template instances (e.g., "P Davie" on pages 1-3 merged with "P Davies" on pages 4-6). Fix: (1) `_deduplicate_records()` now keys on `(name, page_range)` for template docs (`instance_aware=True`), keeping `name`-only for tables (`instance_aware=False`). (2) `EntityResolver.build_confidence()` returns 0.0 for same-document records with different `page_range` (cross-instance merge prevention). Each template instance = one unique person. 7 net new tests.

### Batch Reliability + Configurable Dedup + Dedup UI
- Retry: MAX_RETRIES=3, backoff 2s/4s/8s, split-to-individual on batch failure, unload_unused_models(), timeout_override=120s
- Configurable dedup: _build_anchor_key() with 5 anchors (ssn, name_dob, email, phone, name_address), wired from protocol config
- Analysis API returns {documents, dedup_anchors, protocol_name}, frontend shows read-only anchor checkboxes
- `tests/test_batch_reliability.py`: 30 new tests

### Background Extraction (SSE Decoupling)
- `run_extraction_background()` runs extraction in a daemon thread with its own DB session
- Per-doc commit: PIIRecords serialized to `Document.metadata_json["extracted_records"]` after each doc
- Progress written to `IngestionRun.metrics["extraction_progress"]` with heartbeat
- Resume support: completed_doc_ids tracked, skipped on re-launch; records reloaded from metadata_json
- Cancellation: background thread checks `run.status == "cancelled"` between docs
- `extract_generator()` rewritten as thin SSE relay polling metrics every 2s
- Accepts both `analyzed` (start) and `extracting` (reconnect) status
- Stale heartbeat detection (>60s) re-launches extraction thread with resume
- Frontend: `startExtractStreaming()` auto-reconnects on disconnect (max 60 retries, 2s delay)
- Frontend: "Reconnecting to extraction..." amber status indicator
- 8 new tests in `test_two_phase.py` (serialize/deserialize, progress, relay, reconnect)

### Scanned/image-only PDF support (0 extraction rows)
- **Bug A fix**: `run_extraction_background()` + `analyze_generator()` in `app/pipeline/two_phase.py` -- when `reader.read()` returns empty blocks for a PDF, detect scanned PDF via `fitz.open()` page count. Populate `doc_pages` from PDF page count so Vision path (Path 1) gets actual page numbers to process.
- **Bug B fix**: `ocr_pdf_to_blocks()` added to `app/readers/ocr.py` -- opens PDF with PyMuPDF, renders each page to image at 200 DPI, runs PaddleOCR, returns `list[ExtractedBlock]` compatible with all pipeline paths. Memory-safe (page streaming + `_forget_page()`).
- **Pipeline integration**: Both `analyze_generator()` (structure analysis + onset stages) and `run_extraction_background()` call OCR fallback when blocks are empty for PDF files. Falls back gracefully to Vision-only path if PaddleOCR unavailable.
- `tests/test_scanned_pdf.py`: 18 tests

### Extraction validation -- DOB context, email URLs, entity_types_found
- `validate_dob()` in `app/pii/pattern_validator.py` -- rejects transaction/service/statement dates misclassified as DOB. Checks context keywords, DOB labels, and date recency (within last 5 years = not a DOB).
- `validate_email()` -- rejects URLs (www.*, http://, https://) and strings without @ misclassified as email addresses.
- `_build_entity_types_found()` -- rebuilds entity_types_found from actually-populated PIIRecord fields only.
- Wired into all extraction paths: `VisionDocumentExtractor._data_to_record()` and `LLMTemplateExtractor._data_to_record()`.
- `tests/test_extraction_validation.py`: 40 new tests

### Organization/business name detection in PERSON validation
- `validate_person_name()` in `app/pii/pattern_validator.py` -- two-layer approach.
- **Layer 1 (heuristic)**: `_looks_like_business()` checks business suffixes (INC, LLC, LTD, CORP, etc.), keywords, multi-word patterns, store/branch numbers, firm patterns.
- **Layer 2 (spaCy NER)**: `_spacy_says_org()` for ambiguous cases (4+ word or ALL-CAPS names). Graceful fallback.
- Edge cases: "Estate of John Doe" preserved as PERSON. "ALFRED A. KNOPF, INC." correctly identified as ORG.
- `tests/test_extraction_validation.py`: 43 new tests

### Path 0 coordinate extraction field map quality validation
- **Problem**: Bad field map extracted from page header "Summary Statement" instead of "Client:" anchor, producing 1,354 rows of "Summary".
- **Fix A**: Restored layout_type gate (requires `fixed` or `template_with_drift`).
- **Fix B**: `_validate_field_map()` -- extracts page 0, rejects if PERSON produces known-bad names or single-word names.
- **Fix C**: Schema downgrade prevention -- keeps existing fixed schema if LLM non-deterministically says "variable".
- **Fix D**: Preview validation against `_FIELD_MAP_BAD_NAMES`.
- `tests/test_coordinate_extraction.py`: 8 new tests

---

## Step 19 Sub-Runs

### 19b: Extraction preview in analysis phase
- `extraction_preview` JSON column on DocumentAnalysisReview
- `alembic/versions/0011_extraction_preview.py`: migration
- Preview stage after document_understanding, caches preview, applies to review record
- API returns extraction_preview in GET /jobs/{id}/analysis
- Frontend: ExtractionPreview interface, preview card (fields found/missing, instance count)
- `tests/test_extraction_preview.py`: 9 tests

### 19c: Defensive LLM response parsing (CRITICAL bugfix)
- `_safe_parse_list()` helper, `_parse_table()` helper in llm_document_understanding.py
- `_parse_response()` rewritten -- handles strings/dicts/mixed/nulls for ALL fields. Partial schema on error (never None).
- `_parse_template()` defensive for page_roles as strings
- `_NI_NUMBER_RE` pattern, `detection_to_pii_record()` splits embedded NI numbers from PERSON detections
- `tests/test_defensive_parsing.py`: 13 tests

### 19d: Pipeline fixes for production extraction
- analyze_generator() now passes total_pages/protocol_name/protocol_config to LLM understand
- Subject cleanup: each pipeline run deletes old NotificationSubjects for the project before dedup
- +canonical_dob, +canonical_government_id on NotificationSubject
- `alembic/versions/0012_subject_dob_government_id.py`: migration
- Deduplicator populates canonical_dob and canonical_government_id
- CSV exporter populates from canonical columns
- LLMTemplateExtractor: added IDENTIFICATION_NUMBER + NATIONAL_INSURANCE_UK to _FIELD_TO_RAW and _GOV_ID_TYPES

### 19e: Marker-based instance boundary detection
- `instance_marker` field on DocumentTemplate, `find_instance_boundaries()` method
- LLM prompt asks for `instance_marker` field
- extract_all_instances() prefers marker-based boundaries when set
- `tests/test_template_detection.py`: 4 new tests

---

## Step 21 Sub-Runs

### 21a (Run 1): Layout Assessment + FieldMapping Model
- `FieldMapping` dataclass in `app/structure/document_schema.py`: field_type, anchor_text, spatial_relationship, value_pattern, sample_bbox, line_count, skip_pattern
- `DocumentSchema` extended: +layout_type ("fixed"|"template_with_drift"|"variable"), +layout_field_map (list[FieldMapping]|None), +layout_confidence
- to_dict()/from_dict() roundtrip support, _parse_layout_field_map() defensive parser
- LLM prompts updated with layout analysis instructions
- Safety downgrade if fixed without field_map
- `tests/test_layout_assessment.py`: 25 tests
- **2210 tests passing**

### 21b (Run 2): Coordinate Extractor + Reconciliation
- `app/pipeline/coordinate_extractor.py` NEW: `CoordinateExtractor` class
  - extract_all_pages(page_range?) -> (list[PIIRecord], list[int])
  - Anchor-based: single/multi-word anchors (case-insensitive)
  - Region computation: same_line_right, line_below, lines_below_N, region_right + unknown fallback
  - Skip pattern + value pattern filtering; PERSON mandatory
  - Page streaming: doc._forget_page() for memory
- `app/pipeline/reconciliation.py` NEW: `ExtractionReconciler` class
  - LLM fallback for failed pages; graceful failure (LLM errors -> page dropped)
- `tests/test_coordinate_extraction.py`: 51 tests
- **2262 tests passing**

### 21c (Run 3): Pipeline Wiring
- Coordinate extraction as **Path 0** (before Vision/LLM/Presidio)
- Requires layout_type == "fixed" and layout_field_map populated
- Failed pages -> ExtractionReconciler when llm_assist_enabled
- extraction_path = "0-coord"; existing paths unchanged
- Coordinate preview in analyze_generator()
- API extended with layout_type, layout_field_map, layout_confidence
- `tests/test_two_phase.py`: 8 new tests
- **2271 tests passing**

### 21d (Run 4): Frontend Field Map Editor
- PUT /jobs/{id}/field-map endpoint (validates spatial_relationship values)
- Auditor field map stored on Document.metadata_json["auditor_layout_field_map"]
- Extraction method preference ("coordinate" or "ai") on metadata_json
- Frontend FieldMapEditor component: full CRUD, radio for coord vs AI, per-mapping display/edit
- Pipeline: auditor field map override; "ai" method skips coordinate path
- `tests/test_two_phase.py`: 7 new tests (15 total)
- **2279 tests passing**

### 21e (Run 5): Rotation Awareness + Schema Persistence + PERSON Pattern Fix
- Rotation-aware coordinate extraction (0/90/180/270 degrees)
- Schema persistence: analyze_generator() persists DocumentSchema to metadata_json
- run_extraction_background() loads schema from metadata_json before falling back to LLM
- PERSON fields skip value_pattern validation (names too variable for regex)
- `tests/test_coordinate_extraction.py`: 14 new tests
- `tests/test_two_phase.py`: 4 new tests
- **2293 tests passing**

---

## Step 22 Sub-Runs

### 22a (Run 1): VisionRouter
- `app/pipeline/vision_router.py` NEW: `VisionRouter` + `VisionRoutingResult`
- analyze_document() renders onset page at 200 DPI, sends to vision model
- Routing rules: <=5 pages -> vision_direct, scanned -> vision_direct, fixed_single_page+PII -> coordinate, multi_page_template -> llm_template, table -> llm_table, variable -> presidio
- Graceful fallback on failure -> variable/presidio
- `tests/test_vision_router.py`: 44 tests

### 22b (Run 2): FieldMapBuilder
- `app/pipeline/field_map_builder.py` NEW: `FieldMapBuilder`
- Bridges VisionRoutingResult.pii_fields -> FieldMapping list for CoordinateExtractor
- Fuzzy word matching, deterministic spatial relationships, skip/value pattern inference
- No label = no field map entry
- `tests/test_field_map_builder.py`: 40 tests

### 22c (Run 3): Pipeline Wiring -- Vision Routing Integration
- Vision routing wired into analyze_generator() and run_extraction_background()
- New "vision_routing" stage before coordinate preview
- Priority: auditor override > vision field map > LLM schema field map
- API extended with vision_routing and vision_field_map per document
- `tests/test_two_phase.py`: 11 new tests

### 22d (Run 4): ExtractionVerifier + Frontend Auditor Vision Panel
- `app/pipeline/extraction_verifier.py` NEW: `ExtractionVerifier` + `ExtractionVerification`
- Per-field success rates, ACCEPTABLE_RATE = 0.90, is_acceptable flag
- Post-extraction verification wired into coordinate Path 0
- Frontend: structure type badge, recommended path, PII field count, extraction time estimates
- Vision field map reuses existing FieldMapEditor component
- `tests/test_extraction_verifier.py`: 13 tests
- `tests/test_two_phase.py`: 7 new tests

---

## Test Count Milestones

| Milestone | Count |
|---|---|
| After Step 21a | 2210 |
| After Step 21b | 2262 |
| After Step 21c | 2271 |
| After Step 21d | 2279 |
| After Step 21e | 2293 |
| After all bugfixes | 2787 |
| Current (Step 24e) | ~2800 |
