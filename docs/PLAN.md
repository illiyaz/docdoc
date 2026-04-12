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

---

### Step 37 — Text LLM Batch Extraction (Feature-Flagged)

**Goal:** Reliable extraction path for text PDFs that doesn't depend on coordinate field maps. Simpler, more accurate, acceptable performance (~8-10 min for 225 pages vs 10s coordinate but 20+ min fallback when coordinate fails).

**Rationale:** Coordinate extraction is fast (30-45ms/page) but fragile — depends on LLM producing correct anchor text and spatial relationships, both non-deterministic. When it fails, the fallback chain (table → Presidio) is slow and inaccurate (teacher contamination, name-only records). A text LLM batch path using qwen2.5:7b provides a reliable middle ground.

#### Architecture

**Extraction hierarchy (updated):**
1. **Path 0 — Coordinate** (10s/225 pages): When field map validates. Fastest, most accurate for fixed layouts.
2. **Path 1 — Text LLM Batch** (8-10 min/225 pages): NEW. For text PDFs when coordinate fails. Send 5-10 pages per qwen2.5:7b call with entity_role-aware prompt. ~45 calls × 10-15s each.
3. **Path 2 — Vision** (scanned/image docs only): 90B vision model, ~30s/page.
4. **Path 3 — Presidio** (last resort): Pattern-only, no LLM.

**Feature flag:** `USE_TEXT_LLM_BATCH=true` in .env. When enabled, replaces the table/Presidio fallback for text PDFs.

#### Implementation

| File | What to do |
|---|---|
| `app/pipeline/text_batch_extractor.py` | NEW — TextBatchExtractor class. Accepts page_texts dict, sends batches of 5-10 pages to qwen2.5:7b, parses structured JSON responses into PIIRecord objects. Entity_role-aware prompt distinguishes primary_subject from guardian/provider. |
| `app/llm/prompts.py` | NEW prompt: EXTRACT_TEXT_BATCH — "Given these N pages of text, extract the primary subject's name, address, DOB, SSN, phone for each page. Ignore teacher/provider names." |
| `app/pipeline/two_phase.py` | Wire TextBatchExtractor as Path 1 fallback when coordinate fails and doc has text. Feature-flagged via USE_TEXT_LLM_BATCH setting. |
| `app/core/settings.py` | Add `use_text_llm_batch: bool = False` setting |
| `tests/test_text_batch_extractor.py` | Unit tests: batch building, JSON parsing, entity_role filtering, error handling |

#### Key design decisions
- **Batch size 5-10 pages:** qwen2.5:7b handles ~12K chars comfortably. Each page ~2500 chars. 5 pages = 12.5K chars per call.
- **Entity_role in prompt:** "Extract ONLY the primary subject (student/patient/employee) and their guardian. Ignore institutional names, teacher names, provider names."
- **No field map needed:** The LLM reads the full page text and uses its understanding to find the right names/addresses. More robust than coordinate anchoring.
- **Gap fill also uses text batch:** For text PDFs, gap fill should always use text LLM (7s/page), never vision (30s+/page). Feature flag controls this.
- **Comparison mode:** When both coordinate and text batch are enabled, run both on a sample (5 pages), compare record quality, pick the better path for the full doc.

#### Applicability by document type

Text batch extraction works for ALL doc types — the only variable is the text source:

| Doc type | Text source | Extra step? |
|---|---|---|
| Text PDF | PyMuPDF `page.get_text()` | None |
| XLSX/XLS/CSV | openpyxl/xlrd row data | Serialize rows as text |
| DOCX | python-docx paragraphs | None |
| MSG/EML | extract-msg body + headers | None |
| Scanned PDF | **docTR OCR** (0.55s/page) → text | OCR first |
| HEIC/JPG/PNG | **docTR OCR** → text | OCR first |
| OCR garbage | Vision model (90B) last resort | Only when OCR fails |

This means the 90B vision model is ONLY needed when docTR OCR produces garbage (badly degraded faxes, handwritten forms). Everything else routes through docTR + qwen2.5:7b.

#### Entity_role — primary vs supporting subject

The extraction prompt MUST distinguish primary subjects from supporting cast. This is the single biggest accuracy lever — more important than extraction speed.

**Current prompt coverage (from April 11-12 changes):**
- School docs: student = primary, parent = guardian, teacher = provider ✓
- Generic entity_role: primary_subject, guardian, institutional, provider ✓

**Gaps to fill in Step 37 prompt:**

| Domain | Primary subject | Supporting (DO NOT extract) |
|---|---|---|
| School/FERPA | Student | Teachers, counselors, principal, school staff |
| Medical/HIPAA | Patient | Doctors, nurses, providers, hospital staff, insurance agent |
| Financial/bank | Account holder | Relationship manager, branch staff, co-signers (tag separately) |
| Legal | Plaintiff/defendant/claimant | Attorneys, judges, court clerks, witnesses |
| HR/employment | Employee | HR manager, supervisor, recruiter, reference contacts |
| Insurance | Policyholder/claimant | Agent, adjuster, underwriter, provider |
| Government | Taxpayer/beneficiary | Caseworker, examiner, agent |

The Step 37 prompt should include domain-specific suppression:
```
"If this is a SCHOOL document: extract student name + parent name + student address. 
 Ignore: teacher names (appear in grade sections), counselor names, principal name.
 If this is a MEDICAL document: extract patient name + DOB + MRN + address.
 Ignore: physician names, nurse names, facility staff, insurance company contacts.
 If this is a FINANCIAL document: extract account holder name + SSN + address.
 Ignore: relationship manager, branch manager, agent names.
 ..."
```

#### Feature flag testing plan

1. **`USE_TEXT_LLM_BATCH=false`** (default): Current behavior — coordinate → table → Presidio
2. **`USE_TEXT_LLM_BATCH=true`**: New behavior — coordinate → text batch → Presidio
3. **`USE_TEXT_LLM_BATCH=compare`**: Run both paths on 5 sample pages, log comparison, pick winner

Testing with `scripts/test_llm_judgment.py`:
- Run all 12 phase2_large_pdfs_mini files with each mode
- Compare: subjects found, accuracy (vs ground truth), teacher contamination rate, total time
- Output to `output/extraction_comparison.csv` for analysis

#### Updated extraction hierarchy

```
Has text layer? ──yes──> Coordinate field map validates? ──yes──> PATH 0: Coordinate (10s)
     │                          │ no                                          
     │                          v                                          
     │                   PATH 1: Text LLM Batch (8-10 min)                
     │                          │                                          
     no                         v                                          
     │                   PATH 3: Presidio fallback (12s)                   
     v                                                                     
 docTR OCR ──text OK──> PATH 1: Text LLM Batch                           
     │                                                                     
     no (garbage)                                                          
     v                                                                     
 PATH 2: Vision (90B) ── scanned/image only                               
```

#### Edge cases and multi-page strategy

**Existing multi-page infrastructure (already built, reusable):**
- `DocumentTemplate` with `pages_per_instance` and `instance_marker` — detects multi-page records
- `find_instance_boundaries()` — marker-based scanning for variable-length instances
- `get_instance_pages()` — fixed-stride fallback
- `LLMTemplateExtractor.extract_all_instances()` — batched LLM extraction with dedup
- `build_composite_record()` — Presidio-based cross-page stitching fallback
- **Verdict: highly compatible with text batch.** No reinvention needed — wire text batch into existing instance grouping.

**Edge case 1: Data spanning page boundaries**
- Problem: Address starts on page 14, city/state on page 15
- Solution: **Overlapping page windows.** Send pages [0-5], [4-9], [8-13]... with 1-2 page overlap. The LLM sees complete data; dedup merges duplicates from overlapping windows.
- Alternative: **Tail-buffer stitching** — prepend last 5 lines of previous page to current page text. Cheaper than full overlap.
- The existing `PageStitcher` in `app/readers/stitcher.py` does text-level stitching for coordinate extraction but is NOT used in template extraction. Could be adapted.

**Edge case 2: Multiple subjects on one page**
- Problem: Payroll register has 4-8 employees per page in tabular format
- Solution: The prompt says "extract ALL subjects, return JSON array." The LLM naturally handles this — it reads "John Smith SSN:... Jane Doe SSN:..." and returns `[{person1}, {person2}]`.
- Coordinate extraction CANNOT handle this (fixed to 1 record/page). Text batch can.
- Risk: Dense pages with 20+ subjects — LLM may miss some. QA gap detection catches these.

**Edge case 3: Variable subjects per page**
- Problem: Page 1 has 3 subjects, page 2 has 1, page 3 has 5
- Solution: Text batch returns variable-length arrays per page. The pipeline already handles this — `extract_all_instances()` processes per-instance, and multi-subject pages just return more records.

**Edge case 4: Multi-page single-person (existing template path)**
- Problem: Loan application — page 1 demographics, page 3 SSN, page 5 employment
- Solution: **Already handled** by `LLMTemplateExtractor`. Pages grouped by `pages_per_instance` or `instance_marker`. All pages for one person sent together in one LLM call. No changes needed.
- Text batch enhancement: When `pages_per_instance > 1`, batch by instance groups instead of fixed page windows.

**Edge case 5: Mixed structure within one document**
- Problem: HR file — page 1 is a form (Category A), pages 2-5 are payroll register (Category B), pages 6-10 are emails (Category E)
- Solution: **Per-page classification** during analysis. The segregation step already identifies doc type per page. Text batch can use per-page routing — send form pages individually, send payroll pages as a group, send email pages with full context.
- This is a Phase 8+ capability — not in scope for Step 37 MVP.

#### Performance targets
- 225-page text PDF: ~8-10 min (vs 10s coordinate, 20+ min table fallback)
- Per-page accuracy: >95% name extraction, >90% address extraction
- Entity_role accuracy: <5% teacher/provider contamination (vs ~50% with Presidio)

#### Migration path
1. Implement behind feature flag (off by default)
2. Test on all 12 phase2_large_pdfs_mini files
3. Compare accuracy vs coordinate vs Presidio using test_llm_judgment.py
4. If accuracy ≥ coordinate accuracy: make it the default Path 1
5. Eventually: coordinate becomes optional optimization, text batch is the reliable default
