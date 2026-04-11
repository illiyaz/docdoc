# WORKSTATE — Step 30e: LLM-First Segregation

## Current Sub-step: 30e-7 COMPLETE — ALL 30e SUB-STEPS DONE

## Completed
- [x] **30e-1: LLM Segregation Engine** — DONE
- [x] **30e-2: Document Grouping & Sampling** — DONE
  - Created `app/pipeline/segregation.py`: SegregationEngine + SegregationResult + SegregationField
  - Added SEGREGATION_PROMPT_VISION + SEGREGATION_PROMPT_TEXT to `app/llm/prompts.py`
  - Created `tests/test_segregation.py`: 23 tests passing
  - Features: vision classification (PDF/images), text fallback (XLSX/DOCX/MSG/CSV), page 2 retry, batch mode, role attribution, fallback model support
  - Safety: no raw PII stored in results (field names/types only)
- [x] **30e-3: Segregation Review UI** — DONE
  - Created `app/api/routes/segregation.py`: 6 API endpoints (run, list groups, approve, reject, reclassify, approve-all)
  - Wired router into `app/api/main.py`
  - Created `tests/test_segregation_api.py`: 11 tests (list, approve, reject, reclassify, bulk approve, run w/ mocked engine, router registration)
  - Added segregation types + 7 API functions to `frontend/src/api/client.ts`
  - Created `frontend/src/pages/SegregationReview.tsx`: card layout per group, field chips, role attribution, status badges, approve/reject/reclassify actions, bulk approve, run segregation, summary bar
  - Wired route in `frontend/src/App.tsx`: `/projects/:id/segregation`
  - Full frontend-backend wiring verified: all 5 checks PASS (route registration, prefix consistency, type alignment, route wiring, component imports)
  - Correction memory: reject and reclassify actions persist to JSONL for future few-shot learning
- [x] **30e-4: Role Attribution Plumbing Fix** — DONE
- [x] **30e-5: Correction Memory** — DONE
  - Added `load_segregation_corrections()`, `apply_corrections()`, `_inject_corrections_into_prompt()` to `app/pipeline/segregation.py`
  - SegregationEngine loads corrections on init via `project_id`
  - Corrections applied post-LLM (deterministic overrides) and pre-LLM (few-shot injection)
  - 14 new tests in `tests/test_segregation.py`
- [x] **30e-6: Automated Gap Detection & Fill** — DONE
  - Created `app/pipeline/gap_detector.py`: ExtractionGap dataclass, GapDetector class (page/field/truncation detection)
  - Created `app/pipeline/gap_filler.py`: GapFiller class with 4-path fallback cascade (coordinate_relaxed → llm_template → vision → presidio), LLM budget tracking, regex fallback, value masking, JSON persistence
  - Created `app/api/routes/gaps.py`: 3 endpoints (list, summary, resolve), JSON file persistence, manual resolve/mark_na/mark_unrecoverable
  - Wired gaps_router into `app/api/main.py`
  - Added gap types + 3 API functions to `frontend/src/api/client.ts`
  - Created `tests/test_gap_detection.py`: 53 tests (page gaps, field gaps, truncation, dataclass, masking, LLM parsing, cascade logic, budget enforcement, persistence, safety)
  - All 119 30e-related tests passing (37 segregation + 29 grouping + 53 gap detection)

- [x] **30e-7: Extraction QA Screen** — DONE
  - Created `app/pipeline/qa_sampler.py`: QASampler class with 5 category sampling (largest_group, gap_filled, merged, cross_type, edge_case), QASample dataclass
  - Created `app/api/routes/extraction_qa.py`: 5 endpoints (summary, samples, gaps, resolve, approve), approval gating on high-severity unresolved gaps, JSON state persistence
  - Wired router into `app/api/main.py`
  - Added QA types + 5 API functions to `frontend/src/api/client.ts`
  - Created `frontend/src/pages/ExtractionQA.tsx`: 3-panel layout (summary dashboard, smart sample panel, unresolved gaps panel), gap resolution UI (enter value, mark N/A, mark unrecoverable), approval button with gating
  - Wired route in `frontend/src/App.tsx`: `/projects/:id/qa`
  - Created `tests/test_extraction_qa.py`: 17 tests (sampler categories, dedup, edge cases, safety, approval gating, state persistence, router registration)
  - All 135 30e-related tests passing (37 segregation + 29 grouping + 53 gap detection + 16 QA)
  - Frontend-backend wiring verified: all 5 checks PASS

## Next Steps
- Step 30e is COMPLETE. Proceed to Phase 6 (Security + Governance) per PLAN.md.

## Files Created/Modified in 30e-7
| File | Action | Description |
|---|---|---|
| `app/pipeline/qa_sampler.py` | NEW | QASampler (5-category smart sampling), QASample dataclass |
| `app/api/routes/extraction_qa.py` | NEW | 5 endpoints: summary, samples, gaps, resolve, approve (gated) |
| `app/api/main.py` | MODIFIED | Added extraction_qa_router import + include_router |
| `frontend/src/api/client.ts` | MODIFIED | Added QASample, QASummaryResponse, QAApproveResponse types + 5 API functions |
| `frontend/src/pages/ExtractionQA.tsx` | NEW | 3-panel QA page: summary dashboard, smart samples, gap resolution |
| `frontend/src/App.tsx` | MODIFIED | Added ExtractionQA import + route `/projects/:id/qa` |
| `tests/test_extraction_qa.py` | NEW | 17 tests: sampler, safety, approval gating, state persistence |

## Key Design Decisions (30e-7)
- **Smart sampling, not random:** 5 categories (largest group, gap-filled, merged, cross-type, edge cases) with budget allocation
- **Approval gating:** Cannot approve if unresolved high-severity gaps exist
- **3-panel layout:** Summary → Samples → Gaps, matching the PLAN.md spec
- **Gap resolution actions:** Enter value (masked on save), mark N/A, mark unrecoverable
- **JSON-on-disk QA state:** approval status persisted per job, no migration needed
- **Gap-filled samples use unique IDs:** Prevents dedup collision with largest-group samples
- **All values masked:** QASampler masks all PII fields before including in samples

## Files Created/Modified in 30e-6
| File | Action | Description |
|---|---|---|
| `app/pipeline/gap_detector.py` | NEW | ExtractionGap dataclass, GapDetector class, page/field/truncation detection |
| `app/pipeline/gap_filler.py` | NEW | GapFiller (4-path cascade), FillAttempt, _mask_value, _parse_llm_fill_response, persist/load_gaps |
| `app/api/routes/gaps.py` | NEW | 3 endpoints: list gaps, summary, resolve |
| `app/api/main.py` | MODIFIED | Added gaps_router import + include_router |
| `frontend/src/api/client.ts` | MODIFIED | Added ExtractionGap, GapListResponse, GapSummaryResponse types + 3 API functions |
| `tests/test_gap_detection.py` | NEW | 53 tests covering gap detection, gap filling, persistence, safety |

## Key Design Decisions (30e-6)
- **4-path fallback cascade:** coordinate_relaxed (no LLM) → llm_template (1 LLM call) → vision (1 LLM call) → presidio/regex (no LLM)
- **Budget system:** max 3 LLM calls per gap, max 50 LLM calls total (configurable)
- **Severity-based ordering:** high severity gaps processed first to maximize impact within budget
- **Truncated gaps use shorter cascade:** only coordinate_relaxed + llm_template (no vision/presidio)
- **Stitching gaps marked not_applicable:** require manual review, not auto-fill
- **JSON-on-disk persistence:** gaps saved per job_id, no DB migration needed
- **Value masking:** all filled values masked before storage (SSN → ***-**-6789, etc.)
- **Regex fallback:** when Presidio not available, regex patterns used for SSN, phone, email, name, etc.
- **Manual resolution API:** 3 actions (resolve with value, mark_na, mark_unrecoverable)
- **No raw PII in gap data:** context messages use '***' for values, filled_value_masked always masked

## Files Created/Modified in 30e-1, 30e-2, and 30e-3
| File | Action | Description |
|---|---|---|
| `app/pipeline/segregation.py` | NEW | SegregationEngine, SegregationResult, SegregationField, vision + text classification |
| `app/llm/prompts.py` | MODIFIED | Added SEGREGATION_PROMPT_VISION, SEGREGATION_PROMPT_TEXT, registered in PROMPT_TEMPLATES |
| `tests/test_segregation.py` | NEW | 23 tests: result dataclass, response parsing, engine flows (mocked LLM), safety |
| `docs/WORKSTATE.md` | NEW | This progress tracking file |
| `app/pipeline/grouping.py` | NEW | group_documents(), DocumentGroup, Jaccard-based field similarity clustering, smart sample selection |
| `tests/test_grouping.py` | NEW | 29 tests: Jaccard, naming, splitting, sampling, full grouping (PII/non-PII, multi-type, roles, confidence) |
| `app/api/routes/segregation.py` | NEW | 6 endpoints: run, list groups, approve, reject, reclassify, approve-all. JSON file persistence. Correction memory (JSONL). |
| `app/api/main.py` | MODIFIED | Added segregation_router import + include_router |
| `tests/test_segregation_api.py` | NEW | 11 tests: list, approve, reject, reclassify, bulk approve, run (mocked), empty, router registration |
| `frontend/src/api/client.ts` | MODIFIED | Added SegregationGroup, SegregationGroupsResponse, RunSegregationResponse types + 7 API functions |
| `frontend/src/pages/SegregationReview.tsx` | NEW | GroupCard, StatusBadge, FieldChip components. Summary bar. Approve/reject/reclassify/bulk actions. |
| `frontend/src/App.tsx` | MODIFIED | Added SegregationReview import + route `/projects/:id/segregation` |

## Files Created/Modified in 30e-4
| File | Action | Description |
|---|---|---|
| `app/pii/presidio_engine.py` | MODIFIED | Added `entity_role` field to `DetectionResult` dataclass |
| `app/structure/document_schema.py` | MODIFIED | Added `entity_role` field to `FieldMapping` dataclass |
| `app/pipeline/record_mapper.py` | MODIFIED | `detection_to_pii_record()` copies entity_role; `build_composite_record()` uses majority-vote role |
| `app/pipeline/coordinate_extractor.py` | MODIFIED | Reads entity_role from PERSON field in field_map, passes to PIIRecord |
| `app/structure/llm_template_extractor.py` | MODIFIED | `_data_to_record()` accepts and passes entity_role to PIIRecord |
| `app/structure/vision_extractor.py` | MODIFIED | `_data_to_record()` accepts and passes entity_role to PIIRecord |
| `app/pipeline/field_map_builder.py` | MODIFIED | Reads `role` from vision pii_field dict, sets on FieldMapping.entity_role |
| `app/pipeline/two_phase.py` | MODIFIED | FieldMapping serialization includes `entity_role` (2 locations) |
| `tests/test_role_attribution.py` | NEW | 16 tests: DetectionResult role, record_mapper role propagation, FieldMapping role, merge prevention, safety |

## Key Design Decisions (30e-4)
- **Root cause:** Two disconnected DetectionResult classes (presidio vs tasks/detection). Neither populated entity_role on PIIRecord.
- **Fix approach:** Added entity_role to all 5 extraction paths (Presidio→record_mapper, coordinate, LLM template, vision, composite).
- **Role source chain:** Segregation SegregationField.role → FieldMapBuilder pii_field["role"] → FieldMapping.entity_role → CoordinateExtractor → PIIRecord.entity_role → merge prevention in entity_resolver.
- **Composite record role:** Majority-vote across detections (ignore None values).
- **Backward compat:** All entity_role fields default to None, old field map dicts without entity_role still deserialize correctly.
- **No migration needed:** entity_role is not stored in the DB schema — it's a runtime field on PIIRecord used for merge decisions.

## Key Design Decisions (30e-3)
- Segregation groups persisted as JSON on disk (not in DB) — lightweight, no migration needed
- Route: `/projects/:id/segregation` (nested under project, accessible from ProjectDetail)
- No top-level nav item — accessed from within project context
- Corrections (reject, reclassify) persist to JSONL for future few-shot prompt injection
- `approve-all` only touches `pending_review` groups, skips already approved/rejected
- Frontend uses React Query with `["segregation", projectId, jobId]` query key for cache invalidation
