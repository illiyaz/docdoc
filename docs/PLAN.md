# Implementation Plan — Forentis AI (Active Steps)

For completed steps, see [PLAN_COMPLETED.md](PLAN_COMPLETED.md).
For project overview and conventions, see [../CLAUDE.md](../CLAUDE.md).
For detailed per-step implementation notes, see [CLAUDE_HISTORY.md](CLAUDE_HISTORY.md).

---

## Progress Summary

| Phase | Status | Tests |
|---|---|---|
| Phase 1-4 (Core + RRA + Protocols + HITL) | COMPLETE | ~1500 |
| Phase 5 Steps 1-24e (Extraction Engine) | COMPLETE | ~2800 |
| Phase 5 Steps 26-26d (LiteParse + Auditor Workflow) | COMPLETE | ~2850 |
| Phase 5 Step 29a (Notification Preview) | COMPLETE | ~2850 |
| Phase 5 Steps 30a-30c (Intelligence + Quality) | COMPLETE | ~2850 |
| Phase 5 Step 30d (OCR Tool Evaluation) | COMPLETE | — |
| Phase 5 Step 30e (LLM Segregation + Review UI + Extraction QA) | COMPLETE | ~2850 |
| Phase 5 Step 30f (Performance + Quality Sprint) | **COMPLETE** | ~2850 |
| **Phase 6 (Security + Governance)** | **NEXT** | — |
| Phase 7 (Workflow Completeness) | Pending | — |
| Phase 8 (Scale + Polish) | Pending | — |

**What's built:** 19 tables, 13 migrations, 78K PII records from 34 real docs.

**What's already landed that future steps build on:**
- Source document viewer with bbox overlays (Step 26b)
- Merge explanation with per-anchor signals (Step 26c)
- Notification email/letter preview with masked PII (Step 29a)
- Delivery status dashboard endpoint (Step 26d)
- Analysis review filter tabs, dedup summary, extraction progress bar (Step 26d)
- Intelligence tab: document understanding diagnostic, test-extract, correction memory (Step 30a)
- Extraction quality: phone validation (5 paths), name quality gate, DL regex (Step 30b)
- LLM prompt coverage: FERPA/HR docs, PERSON+LOCATION-only field maps (Step 30c)
- OCR evaluation complete: docTR selected (Apache 2.0, 16x-767x faster, 98.8% SSN coverage on 500 pages) (Step 30d)

---

## OCR Tool Evaluation — Findings & Architecture (April 2026)

**Goal:** Evaluate open-source document parsing tools to determine the optimal extraction stack for Forentis AI, replacing/complementing the current PyMuPDF + PaddleOCR + Ollama vision pipeline.

### Tools Evaluated

| Tool | Version | License | Speed (44 files) | Word-Level BBox | Table Reconstruction | Air-Gap |
|---|---|---|---|---|---|---|
| **docTR (Mindee)** | 1.0.1 | Apache 2.0 | **40.5s** (16x fastest) | ✅ Yes (normalized 0-1) | ❌ No | ✅ ~200-400MB models |
| **Surya (Datalab)** | 0.17.x | GPL + model license ($2M) | 672.4s | ✅ Yes (line-level) | ❌ No (layout labels only) | ✅ ~500MB models |
| **Marker (Datalab)** | 1.10.2 | GPL + model license ($2M) | 356.3s (11 files) | ❌ No (markdown output) | ✅ Yes | ✅ Wraps Surya models |
| **MinerU (OpenDataLab)** | 1.3.12 | **AGPL-3.0** | Testing in progress | ✅ Block-level (0-1000 range) | ✅ Yes (HTML tables) | ✅ ~1-2GB models |

### Key Findings

**docTR is the strongest candidate for bulk OCR extraction:**
- 16x faster than Surya across all document categories
- Word-level bounding boxes (normalized 0-1 coordinates) — ideal for coordinate extraction
- Apache 2.0 license — no revenue caps, no AGPL copyleft concerns
- PyTorch-based, MPS/CUDA/CPU support
- Perfect label-value separation on structured forms (Categories A, H, O)
- Found MORE data than Surya on tabular documents (Category J: 4x more MRNs, 2x employee IDs)

**Surya excels at layout classification:**
- Layout labels (SectionHeader, Table, Text, ListItem) useful for document structure analysis
- Line-level bboxes with reading order
- Slower but higher-level semantic information per line

**All OCR tools share the same weaknesses:**
- Category Q (fillable forms): Form field data in annotation layer, not visible to OCR → solution: AcroForm extraction
- Category U (batch letters): Narrative text, no clear label-value pairs → solution: LLM
- Category M (financial instruments): Short docs with decorative elements → solution: LLM
- Category J (system reports): Dense columnar data with merged columns → solution: coordinate/column mapper

### Revised Architecture Decision: LLM-First Segregation

**Key insight:** File segregation (PII vs non-PII classification) is a **comprehension task**, not an extraction task. OCR tools are the wrong layer.

**Segregation flow (per file):**
1. Render page 1-2 as images (any rasterizer)
2. Send to qwen2.5vl:32b vision LLM: "Does this contain PII? What type of document? List PII types and primary subject."
3. LLM returns: PII yes/no + document type + field inventory + **role attribution** (primary subject vs secondary contacts)
4. Group by document type → sample for full extraction

**Why LLM-first for segregation:**
- One call gives PII detection + doc type + field inventory + role hints — four tasks in one
- Understands context: distinguishes SSN in a form from a random 9-digit number
- No regex false positives
- Works on native text, scanned, and image files equally
- Already deployed locally (no new infrastructure)
- ~2-3 seconds per file, 1,000 files in under an hour

**Extraction flow (after segregation):**
- Structured forms/tables (A, H, J, O): docTR word-level OCR → coordinate extraction → field map
- Narrative documents (U, K, M): LLM direct extraction (short docs, high accuracy)
- Fillable forms (Q): AcroForm/annotation layer extraction (no OCR needed)
- Scale documents (225+ pages): docTR batch processing (25 pages/batch, memory-managed)

### Role Attribution via LLM Semantic Field Map

**Problem:** Entity role (primary_subject vs secondary_contact) plumbing exists in codebase but is broken — role flows from structure analysis → DetectionResult but never reaches PIIRecord (record_mapper.py drops it).

**Solution (two parts):**
1. **Plumbing fix:** Wire `entity_role` from DetectionResult through to PIIRecord in `record_mapper.py`. Merge prevention logic in entity_resolver.py is already coded, just starved of data.
2. **LLM semantic field map:** During segregation/first-look, LLM assigns roles to field types: `"Student Name" = primary_subject`, `"Parent Name" = secondary_contact`. This role travels with the field map through FieldMapBuilder → coordinate extraction → PIIRecord.

### Scale Testing Results

**Full 225-page documents (docTR vs Surya):**

| File | docTR | Surya | Speed Ratio | PII Parity |
|---|---|---|---|---|
| WashingtonCMD (225 pages) | 80.7s | 3,018.9s | 37x faster | Same SSN/phone/email counts |
| CMG Inc (225 pages) | 36.8s | 28,240.4s | 767x faster | Same accuracy |

Surya had catastrophic performance degradation on CMG (batches 131-190 went from ~27s to 3,900-6,300s per batch due to memory/thermal throttling). Production reliability risk.

**Multi-page completeness (500-page Complex1.pdf with docTR):**
- 145.33s for 500 pages (0.29s/page). Zero empty pages, zero low-content pages.
- 988 SSNs found across 494/500 pages (98.8%). 6 pages without SSN = genuine data variation, not OCR miss.
- 808 phones on 477 pages (95.4%), 336 emails on 285 pages (57%).
- Consistent word counts across all 500 pages (min=194, max=311, avg=245). No outliers.
- Cross-page boundary analysis: all pages start with "Inst 747 / MIDDLEFIELD BANKING COMPANY" header — clean boundaries for stitcher.

**Conclusion:** docTR is production-ready. Surya abandoned for extraction (retained optionally for layout classification).

### Test Scripts

| Script | Tool | Status |
|---|---|---|
| `scripts/test_doctr_all_categories.py` | docTR (44 files, A-X + real-world) | ✅ Complete: 44/44, 40.5s |
| `scripts/test_doctr_category_a.py` | docTR (11 Category A files) | ✅ Complete: 11/11, 8.6s |
| `scripts/test_scale_comparison.py` | docTR + Surya (225-page docs) | ✅ Complete: docTR 37-767x faster |
| `scripts/test_multipage_completeness.py` | docTR + Surya (multi-page continuation) | ✅ Complete: 500 pages, 0 gaps |
| `scripts/test_mineru_all_categories.py` | MinerU (44 files, A-X + real-world) | ❌ Abandoned: model arch mismatch + AGPL |
| `scripts/test_marker_category_a.py` | Marker (11 Category A files) | ✅ Complete: 9/11, 356.3s |

### Licensing Summary

| Tool | License | Revenue Cap | Copyleft Risk | Forentis Impact |
|---|---|---|---|---|
| docTR | Apache 2.0 | None | None | ✅ Safe for commercial SaaS |
| Surya | GPL + model | $2M org revenue | GPL copyleft | ⚠️ Manageable (willingness to pay) |
| Marker | GPL + model | $2M org revenue | GPL copyleft | ⚠️ Same as Surya (wraps Surya) |
| MinerU | AGPL-3.0 | None | **Network copyleft** | ❌ Requires open-sourcing API layer |

### LLM Judge — Multi-Engine Quality Assurance (Future)

**Concept:** When multiple OCR engines are available (docTR primary, Surya optional), an LLM judge compares outputs and selects the best result per page. Integrated into the existing first-look LLM call — the segregation call already sees page images, so it can also evaluate OCR quality if needed.

**When it activates:** Only when extraction quality is uncertain (low confidence, dense tables, mixed layouts). Not for every page — that would negate docTR's speed advantage.

**Implementation:** Deferred to Phase 8. docTR alone achieves 98.8% SSN coverage and zero empty pages on 500-page docs. Judge adds value only at the margins.

---

## Step 30e — LLM-First Segregation + Segregation Review UI

**Goal:** Replace OCR+regex file classification with vision LLM first-look. Add a new UI screen for auditors to review and approve document groupings before full extraction.

**Two modes:**
- **Folder mode (bulk):** Full segregation → grouping → auditor review → bulk extraction
- **Single-file mode:** Segregation → skip grouping UI → direct analysis → extraction (existing flow, enhanced with LLM first-look)

### 30e-1. LLM Segregation Engine

| File | What to do |
|---|---|
| `app/pipeline/segregation.py` | NEW — `SegregationEngine.classify(file_path) → SegregationResult`. Renders page 1-2 as images → qwen2.5vl:32b vision LLM. Returns: pii (bool), document_type, field_inventory, role_attribution (dict mapping field names to primary_subject/secondary_contact). Fallback to llama3.2-vision. |
| `app/pipeline/segregation.py` | `SegregationResult` dataclass: pii_detected, document_type, confidence, field_inventory (list[str]), role_map (dict[str, str]), page_count, file_type, llm_model_used, processing_time_ms |
| `app/llm/prompts.py` | Add SEGREGATION_PROMPT — structured JSON output: `{pii: bool, document_type: str, fields: [{name, type, role}], confidence: float}` |
| `app/pipeline/two_phase.py` | Add segregation as Stage 0 before discovery. For single-file mode, run segregation inline and skip grouping. |
| `tests/test_segregation.py` | PII vs non-PII classification, document type accuracy, role attribution, fallback model, timeout handling |

### 30e-2. Document Grouping & Sampling

| File | What to do |
|---|---|
| `app/pipeline/grouping.py` | NEW — `group_documents(results: list[SegregationResult]) → list[DocumentGroup]`. Groups by document_type + field_inventory similarity. Picks representative samples per group (3-5 files). |
| `app/pipeline/grouping.py` | `DocumentGroup` dataclass: group_id, document_type, file_count, sample_file_ids, field_inventory, role_map, confidence_avg |
| `app/db/models.py` | Add `SegregationResult` table: id, project_id, document_id, pii_detected, document_type, confidence, field_inventory_json, role_map_json, llm_model, processing_time_ms, created_at. Add `DocumentGroup` table: id, project_id, group_name, document_type, file_count, sample_doc_ids_json, status (pending_review/approved/rejected), reviewed_by, reviewed_at |
| `alembic/versions/0014_segregation.py` | Migration for segregation_results + document_groups tables |
| `tests/test_grouping.py` | Grouping logic, sample selection, edge cases (1 file, all same type, all different) |

### 30e-3. Segregation Review UI

| File | What to do |
|---|---|
| `app/api/routes/segregation.py` | NEW — `GET /projects/{id}/segregation/groups` (list groups with sample previews), `GET /projects/{id}/segregation/groups/{gid}/samples` (sample file details with page thumbnails), `POST /projects/{id}/segregation/groups/{gid}/approve` (approve group), `POST /projects/{id}/segregation/groups/{gid}/reject` (reject/exclude group), `POST /projects/{id}/segregation/groups/{gid}/reclassify` (move file to different group), `POST /projects/{id}/segregation/approve-all` (bulk approve) |
| `frontend/src/pages/SegregationReview.tsx` | NEW — Card layout per group: document type badge, file count, field inventory chips, role attribution summary, 3-4 thumbnail page images from samples. Actions: Approve, Reject, Expand (see all files). Non-PII group shown separately with "Rescue" action. |
| `frontend/src/components/GroupCard.tsx` | NEW — Single group card: thumbnail grid, field chips, confidence bar, approve/reject buttons |
| `frontend/src/components/PageThumbnail.tsx` | NEW — Renders page image with LLM field overlay (field names + roles colour-coded) |
| `frontend/src/App.tsx` | Add route: `/projects/:id/segregation` |
| `tests/test_segregation_api.py` | CRUD for groups, approve/reject flow, reclassify, bulk approve |

### 30e-4. Role Attribution Plumbing Fix

| File | What to do |
|---|---|
| `app/pipeline/record_mapper.py` | Wire `entity_role` from DetectionResult → PIIRecord. Copy `detection.entity_role` in `detection_to_pii_record()`. |
| `app/pipeline/field_map_builder.py` | Enrich FieldMapping entries with `role` from segregation's role_map. Match by field name/type. |
| `app/pipeline/coordinate_extractor.py` | Pass role from FieldMapping → extracted PIIRecord.entity_role |
| `app/rra/entity_resolver.py` | Verify cross-role merge prevention fires correctly now that entity_role flows through |
| `tests/test_role_attribution.py` | End-to-end: LLM assigns role → flows through extraction → RRA respects role boundaries |

### 30e-5. Correction Memory for Segregation

| File | What to do |
|---|---|
| `app/pipeline/segregation.py` | `apply_corrections(result, corrections)` — applies stored corrections from previous runs (reclassifications, rescued non-PII files) |
| Correction JSONL | Store in `project_dir/segregation_corrections.jsonl`. Format: `{file_hash, old_type, new_type, old_pii, new_pii, corrected_by, timestamp}` |
| `app/llm/prompts.py` | Inject corrections as few-shot examples in SEGREGATION_PROMPT for this project |

### 30e-6. Automated Gap Detection & Fill

**Goal:** After extraction, identify every missing field and page-level gap. Automatically attempt recovery before showing anything to the auditor. Only genuinely unrecoverable gaps reach the human.

**Gap detection (runs automatically after extraction):**
- **Page-level gaps:** Compare pages that yielded records vs total pages. Flag pages with zero records on a repeating template.
- **Field-level gaps:** Compare extracted fields per page against the DocumentSchema field inventory. If a page should have SSN + Name + Address but only has Name + Address, that's a field gap.
- **Truncation detection:** Flag records that look incomplete — name with no last name, address with only a street number, phone with fewer than 10 digits.
- **Cross-page stitching gaps:** Detect records at page boundaries where data may be split (stitcher TAIL_BUFFER_LINES=5 should catch most, but verify).

**Auto-fill (runs automatically on detected gaps):**
- For each missing field on a flagged page, attempt targeted re-extraction through fallback paths:
  1. Re-run coordinate extraction with relaxed anchor matching (allow drift)
  2. LLM template extraction on just that page, asking specifically for the missing field
  3. Vision direct on that page, targeted prompt: "Page 48 should have an SSN near [anchor]. What do you see?"
  4. Presidio NER as final fallback
- Budget: max 3 LLM calls per gap. If all fail, gap is marked `unrecoverable` and queued for manual review.
- Successfully recovered values get `extraction_method = "gap_fill"` + which fallback path succeeded.

| File | What to do |
|---|---|
| `app/pipeline/gap_detector.py` | NEW — `GapDetector.detect(job_id) → list[ExtractionGap]`. Runs page-level, field-level, and truncation checks. |
| `app/pipeline/gap_detector.py` | `ExtractionGap` dataclass: document_id, page_num, expected_field, actual_fields, gap_type (missing_field/empty_page/truncated/stitching), severity (high/medium/low) |
| `app/pipeline/gap_filler.py` | NEW — `GapFiller.fill(gaps: list[ExtractionGap]) → GapFillReport`. Iterates through fallback extraction paths per gap. Returns: filled (list), unfilled (list), stats. |
| `app/pipeline/two_phase.py` | Add gap detection + fill as post-extraction stage. Runs automatically after bulk extraction completes. |
| `app/db/models.py` | Add `ExtractionGap` table: id, job_id, document_id, page_num, expected_field, gap_type, severity, fill_attempted (bool), fill_method, fill_result (filled/unfilled/not_applicable), filled_by (system/manual), reviewed_by, reviewed_at, notes |
| `alembic/versions/` | Migration for extraction_gaps table |
| `tests/test_gap_detection.py` | Page-level gaps, field-level gaps, truncation, auto-fill success/failure |

### 30e-7. Extraction QA Screen + Manual Gap Review

**Goal:** After automated extraction + gap-fill, present the auditor with a confidence-building QA view. Auditor reviews completeness, inspects sample records, resolves remaining gaps, and approves for notification.

**Extraction QA screen layout:**

**Top section — Summary dashboard:**
- Total notification subjects identified, total documents processed, total pages
- Extraction completeness: "98.3% of pages produced records, 12 field gaps auto-filled, 3 unrecoverable gaps"
- Per-document-group breakdown: "Banking Statements: 200 docs, 45,000 records, 99.1% complete"

**Middle section — Smart sample panel:**
- Not random sampling — curated selection that builds confidence:
  - Records from the largest document group (bulk of the work)
  - Records that were gap-filled (show original gap + recovery method)
  - Records merged by RRA (show merge explanation from Step 26c)
  - Records from different document types (cross-group coverage)
  - Edge cases: shortest records, lowest confidence, most fields extracted
- Each sample shows: extracted fields + source page image with bbox overlays (Step 26b viewer) side-by-side

**Bottom section — Unresolved gaps panel:**
- List of gaps that auto-fill couldn't recover
- For each gap: source page image, what was expected, what extraction paths were tried, why they failed
- Auditor actions per gap:
  - **"Enter Value"** — auditor types the value they can see on the source image. Creates PIIRecord with `extraction_method = "manual"`, `created_by = auditor_id`. Record flows through normalization → RRA → notification subject (merges if matching subject exists, creates new if not). Full audit trail.
  - **"Not Applicable"** — field genuinely isn't on this page (legitimate data variation). Logged with reason, excluded from gap count. Feeds into correction memory for future runs.
  - **"Unrecoverable"** — field is there but unreadable (damaged scan, smudge, low resolution). Logged with note. Notification subject may still exist from other pages with fewer data points.

**Approval action:**
- "Approve for Notification" — acknowledges known gaps (if any), signs off on completeness
- Approval includes: auditor ID, timestamp, gap summary at time of approval, total subjects
- Gated: cannot approve if there are unresolved high-severity gaps (must be addressed or explicitly marked N/A/unrecoverable)

**Manual entry → notification subject flow:**
- Manual PIIRecord → normalization (same phone/email/name normalizers) → RRA entity resolution (may merge with existing subject) → notification subject list updated
- Audit log: "SSN 939-48-7036 manually entered by [auditor] on page 48, merged with Subject #412"
- Manual entries are flagged in exports so legal team knows which values were human-entered vs machine-extracted

| File | What to do |
|---|---|
| `app/api/routes/extraction_qa.py` | NEW — `GET /jobs/{id}/qa/summary` (completeness stats), `GET /jobs/{id}/qa/samples` (curated sample records with source page refs), `GET /jobs/{id}/qa/gaps` (unresolved gaps with source images), `POST /jobs/{id}/qa/gaps/{gap_id}/resolve` (enter value, mark N/A, mark unrecoverable), `POST /jobs/{id}/qa/approve` (final approval) |
| `app/pipeline/qa_sampler.py` | NEW — `QASampler.select_samples(job_id) → list[QASample]`. Smart sampling: largest group, gap-filled, merged, cross-type, edge cases. Each sample includes record + source page ref + bbox. |
| `app/pipeline/manual_entry.py` | NEW — `ManualEntryHandler.create_record(gap_id, field_type, raw_value, auditor_id) → PIIRecord`. Normalizes, creates PIIRecord (extraction_method="manual"), runs through RRA, updates notification subject. |
| `frontend/src/pages/ExtractionQA.tsx` | NEW — Three-panel layout: summary dashboard (top), sample records with source viewer (middle), unresolved gaps with resolve actions (bottom). |
| `frontend/src/components/GapResolveModal.tsx` | NEW — Modal for gap resolution: shows source page image, extraction attempts, input field for manual value, N/A and Unrecoverable buttons with reason field. |
| `frontend/src/components/QASampleCard.tsx` | NEW — Record card with extracted fields + embedded DocumentViewer showing source page with bbox highlights. |
| `frontend/src/App.tsx` | Add route: `/jobs/:id/qa` |
| `tests/test_extraction_qa.py` | QA summary stats, smart sampling, gap resolution (all 3 options), manual entry → RRA flow, approval gating |

---

## Phase 6 — Security + Governance

**Goal:** Make the tool deployable. No law firm adopts a tool that handles PII without login.

---

### Step 25 — Authentication, RBAC & Access Audit Logging

**Goal:** Every API call requires a verified identity. Every action is logged. The four roles are enforced.

#### 25a. Auth Backend — JWT + Local User Store

| File | What to do |
|---|---|
| `app/db/models.py` | Add `User` model: id, email, hashed_password, display_name, role, is_active, created_at, last_login_at |
| `alembic/versions/0014_users.py` | Migration for `users` table |
| `app/core/auth.py` | NEW — `hash_password()`, `verify_password()` (bcrypt), `create_access_token()`, `decode_token()` (PyJWT). Token expiry via settings. |
| `app/core/settings.py` | Add `jwt_secret_key`, `jwt_expiry_minutes=480`, `auth_enabled=true` |
| `app/api/routes/auth.py` | NEW — `POST /auth/login`, `POST /auth/register` (admin-only), `GET /auth/me`, `POST /auth/change-password` |
| `app/api/deps.py` | Add `get_current_user()` dependency — extracts JWT from `Authorization: Bearer`, returns `User` or 401 |
| `tests/test_auth.py` | Login, token expiry, bad credentials, password hashing |

**Air-gap safe:** No OAuth, no external IdP. Local user store. JWT secret from settings.

#### 25b. RBAC Enforcement on All Routes

| Role | Can do |
|---|---|
| `QC_SAMPLER` | Read-only: review queue, sampling results |
| `REVIEWER` | Above + approve/reject documents, review subjects |
| `LEGAL_REVIEWER` | Above + escalation decisions, regulatory protocol changes |
| `APPROVER` | Above + extraction, export, notifications, user management |

| File | What to do |
|---|---|
| `app/core/auth.py` | Add `require_role(*roles)` dependency factory — 403 if insufficient |
| All route files | Add `Depends(require_role(...))` to each endpoint |
| `tests/test_rbac.py` | Each role against each endpoint — verify 403 for insufficient privileges |

**Note:** `app/api/routes/documents.py`, `notifications.py`, `review.py` already have `# Phase 6: add Depends(get_current_user)` comments marking where auth goes.

#### 25c. Access Audit Logging

| File | What to do |
|---|---|
| `app/db/models.py` | Add `AccessLog` model: id, user_id, action, resource_type, resource_id, ip_address, timestamp |
| `alembic/versions/0015_access_logs.py` | Migration (append-only — no DELETE/UPDATE) |
| `app/api/middleware/access_log.py` | NEW — logs every authenticated request |
| `app/api/routes/audit.py` | Add `GET /audit/access-log` — paginated, APPROVER only |
| `tests/test_access_log.py` | Verify entries for all sensitive actions |

---

### Step 27 — Regulatory Deadline Dashboard

**Goal:** Countdown timers per active matter. The #1 number in breach response.

**Prerequisites:** Step 26d delivery dashboard already exists (endpoint + summary).

#### 27a. Breach Date Tracking

| File | What to do |
|---|---|
| `app/db/models.py` | Add to `Project`: `breach_discovered_at`, `breach_occurred_at` (DateTime, nullable) |
| `alembic/versions/` | Migration adding date columns |
| `app/api/routes/projects.py` | Accept breach dates in project create/update |
| `app/protocols/protocol.py` | Add `compute_deadline(discovery_date)` and `days_remaining(discovery_date)` |
| `tests/test_deadlines.py` | Test per-protocol deadlines (HIPAA 60d, GDPR 72h, state laws vary) |

#### 27b. Deadline Dashboard API & Frontend

| File | What to do |
|---|---|
| `app/api/routes/dashboard.py` | Add `GET /dashboard/deadlines` — active projects with days_remaining, status (on_track/at_risk/overdue) |
| `frontend/src/components/DeadlineCountdown.tsx` | NEW — "23 days remaining" or "OVERDUE by 4 days" |
| `frontend/src/pages/Dashboard.tsx` | Deadline widget: matters sorted by urgency, colour coded (green >14d, amber 3-14d, red <3d, black overdue) |
| `frontend/src/pages/ProjectDetail.tsx` | Deadline countdown at top of project view |
| `tests/test_dashboard_deadlines.py` | Deadline API, sorting, status computation |

---

## Phase 7 — Workflow Completeness

**Goal:** Complete the auditor workflow loop. Evidence packaging, batch send, re-extraction, manual merge/split.

---

### Step 28 — Evidence Package Export

**Goal:** One-click export: methodology PDF + notification XLSX + audit CSV + QC report → ZIP.

#### 28a. Methodology Report

| File | What to do |
|---|---|
| `app/export/methodology_report.py` | NEW — auto-generated PDF: engagement summary, document inventory, extraction methodology (which paths used per doc), verification results, dedup summary, QC results, notification summary |
| `app/export/evidence_bundle.py` | NEW — orchestrates: methodology PDF + notification XLSX + audit CSV → ZIP |
| `app/api/routes/exports.py` | Add `POST /exports/{job_id}/evidence-bundle` |
| `frontend/src/pages/ProjectDetail.tsx` | "Export Evidence Bundle" button |
| `tests/test_evidence_bundle.py` | Bundle generation, verify all files present |

#### 28b. XLSX Multi-Sheet Export

| File | What to do |
|---|---|
| `app/export/xlsx_exporter.py` | NEW — Sheet 1 "Notification List", Sheet 2 "Extraction Detail", Sheet 3 "Document Inventory", Sheet 4 "Audit Trail". openpyxl. |
| `app/api/routes/exports.py` | Add `format=xlsx` parameter |
| `tests/test_xlsx_export.py` | Multi-sheet generation, data integrity |

---

### Step 29b — Batch Approval & Send

**Goal:** APPROVER sign-off before batch notification delivery.

**Prerequisites:** Step 29a (notification preview) already complete.

| File | What to do |
|---|---|
| `app/api/routes/notifications.py` | `POST /notifications/lists/{list_id}/approve` (APPROVER role), `POST /notifications/lists/{list_id}/send` (trigger delivery) |
| `frontend/src/pages/ProjectDetail.tsx` | Notification tab: preview samples, approve batch, trigger send, show progress (sent/failed/total) |
| `app/audit/audit_log.py` | Log approval + send events |
| `tests/test_notification_batch.py` | Approval flow, send triggering, audit logging |

**Note:** `NotificationList` model already has `approved_at` and `approved_by` columns. `EmailSender.send_all()` should check `status == "APPROVED"` before proceeding.

---

### Step 30 — Per-Document Re-Extraction

**Goal:** Edit field map → re-extract single doc → verify. No need to re-run entire job.

**Prerequisites:** Document viewer (Step 26b) shows source page for verification. Field map editor (Step 21d) already exists.

| File | What to do |
|---|---|
| `app/api/routes/jobs.py` | `POST /jobs/{job_id}/extract/{doc_id}` — SSE stream for single doc. Accepts optional field_map override. |
| `app/pipeline/two_phase.py` | Extract per-doc logic into `_extract_single_document()` |
| `frontend/src/pages/ProjectDetail.tsx` | "Re-extract" button per document. Show progress inline. Replace old results atomically. |
| `tests/test_reextraction.py` | Single-doc re-extraction, field map override, result replacement |

---

### Step 31 — Manual Entity Merge/Split

**Goal:** Auditor links/unlinks notification subjects with rationale, logged in audit trail.

**Prerequisites:** Merge explanation (Step 26c) shows WHY records were merged. Document viewer (Step 26b) lets auditor see source pages to verify.

| File | What to do |
|---|---|
| `app/api/routes/subjects.py` | NEW — `POST /subjects/merge` (merge 2+ subjects), `POST /subjects/split` (split 1 subject by extraction groups) |
| `app/rra/deduplicator.py` | `merge_subjects()` and `split_subject()` methods. Update PersonEntity links. |
| `app/audit/audit_log.py` | Log merge/split with before/after state |
| `frontend/src/pages/SubjectDetail.tsx` | "Merge with..." (search + confirm) and "Split" (select extractions to separate) |
| `tests/test_manual_merge_split.py` | Merge 2→1, split 1→2, data preservation, audit trail |

---

## Phase 8 — Scale & Polish

---

### Step 32 — Prefect Orchestration & Parallel Extraction

**Goal:** Replace monolithic generator with Prefect tasks. Each doc extracts independently. Failures don't block others.

| File | What to do |
|---|---|
| `app/pipeline/dag.py` | Wire actual Prefect tasks. Per-document extraction as independent tasks. |
| `app/core/settings.py` | `max_parallel_extractions=4`, `extraction_timeout_minutes=30` |
| `tests/test_orchestration.py` | Parallel extraction, failure isolation, timeout |

---

### Step 33 — Multi-Matter Portfolio Dashboard

**Goal:** Firm-level view: "6 active matters, 2 past deadline, 12K subjects total."

| File | What to do |
|---|---|
| `app/api/routes/dashboard.py` | `GET /dashboard/portfolio` — aggregate across all projects |
| `frontend/src/pages/Dashboard.tsx` | Portfolio cards, charts (subjects by protocol, notification progress) |
| `tests/test_portfolio.py` | Aggregation across projects |

---

### Step 34 — Per-Project False Positive Deny List

**Goal:** "Ignore 'Washington' as PERSON in this project" — persists across re-runs.

| File | What to do |
|---|---|
| `app/db/models.py` | `ProjectDenyList` model: project_id, entity_type, value, reason, created_by |
| `app/pii/presidio_engine.py` | Accept deny_list, filter matches before returning |
| `app/api/routes/projects.py` | CRUD `/projects/{id}/deny-list` |
| `frontend/src/pages/ProjectDetail.tsx` | Deny list panel + "Add to deny list" on false positives |
| `tests/test_deny_list.py` | CRUD, filtering, persistence |

---

### Step 35 — PDF Report Export

**Goal:** Formatted PDF notification list + methodology for regulatory filings.

| File | What to do |
|---|---|
| `app/export/pdf_report.py` | NEW — WeasyPrint-rendered PDF with header/footer, page numbers, masked data tables |
| `app/api/routes/exports.py` | Add `format=pdf` parameter |
| `tests/test_pdf_report.py` | PDF generation, masked data, formatting |

---

### Step 36 — UX Polish + Layman-Friendly Interface

**Goal:** Make the UI usable by non-technical users (paralegals, compliance officers). A layman should understand every screen without training.

#### 36a. Jobs Page Redesign

- **Guided workflow:** Replace raw job list with a step-by-step wizard — Upload → Segregation Review → Analysis → Extraction → QA → Notify
- **Progress indicators:** Clear visual pipeline (stepper/breadcrumb) showing where each job is in the workflow
- **Plain-English status:** Replace "Ready to Extract" with "Analysis complete — review results before extracting". Replace "Running" with "Analyzing documents (3 of 12 complete)"
- **Hide failed/cancelled clutter:** Collapsed by default, accessible via filter. Show only active/recent jobs prominently
- **One-click actions:** Big primary button for the next logical step (not "Extract" buried in a row)
- **Time estimates:** "~5 minutes remaining" based on pages/model speed

#### 36b. Dashboard + First-Run Experience

- **Empty state guidance:** When no projects exist, show "Welcome to Forentis AI" with a guided setup flow
- **Dashboard cards:** Matter summary with traffic-light status (green = on track, amber = approaching deadline, red = overdue)
- **Notification progress ring:** Visual showing "234 of 500 subjects notified"
- **Recent activity feed:** "3733050.pdf — 12,450 records extracted 2 hours ago"

#### 36c. Visual Design System

- **Consistent color palette:** Define primary, secondary, success, warning, danger colors
- **Typography hierarchy:** Clear heading levels, readable body text, monospace only for data
- **Card-based layouts:** Replace dense tables with scannable cards where appropriate
- **Loading states:** Skeleton loaders instead of spinners, progress bars with context
- **Error states:** Friendly messages ("We couldn't read page 47 — it may be a scanned image") not stack traces
- **Mobile responsive:** At minimum, dashboard and job status should work on tablet

#### 36d. Contextual Help + Tooltips

- **Field-level help:** Hover tooltips explaining what each PII type means, what each status means
- **Inline documentation:** "What is onset detection?" links/tooltips on the analysis review screen
- **Protocol explainers:** Plain-English summary of what each regulatory protocol requires
- **Keyboard shortcuts:** Document and display common actions (approve, reject, next record)
