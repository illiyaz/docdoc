# Implementation Plan — Forentis AI (Active Steps)

Active implementation steps (21+). For completed steps (Phases 1-4, Steps 1-20), see [PLAN_COMPLETED.md](PLAN_COMPLETED.md).
See [CLAUDE.md](../CLAUDE.md) for project overview and conventions.

**Phase 5 — Forentis AI Evolution (IN PROGRESS)**

Steps 1-24 COMPLETE (2787+ tests). See PLAN_COMPLETED.md for full details.

---

### Step 21 — Coordinate-Based Extraction for Structured Documents (PENDING)

**Goal:** For well-structured documents with fixed/repeating layouts (labeled forms, accounting statements, payslips), use LLM to analyze layout once, then Python coordinate extraction for all pages. This processes 1,354 pages in ~10 seconds instead of 5+ hours via LLM. The LLM acts as the "architect" (reads layout, builds field map), Python acts as the "executor" (extracts from every page using coordinates). Auditor reviews and can edit the field map before extraction runs.

**This is ADDITIVE — does not change existing paths:**

| Layout type | Example | Extraction path | Changed? |
|---|---|---|---|
| Fixed layout | Accounting statements, payslips | **NEW: coordinate extraction** | NEW |
| Template | Pension statements (multi-page per person) | LLM template extraction | Unchanged |
| Tabular | Student rosters, employee lists | LLM table extraction | Unchanged |
| Variable | Letters, free-text, mixed | LLM page extraction | Unchanged |

The pension PDF (149 individuals, 453 pages) continues using the LLM template path exactly as it works today.

---

#### 21a. Layout Assessment — LLM classifies the document

**During analysis phase, LLM outputs layout_type alongside DocumentSchema:**

```python
# Addition to DocumentSchema
layout_type: str = "variable"        # "fixed" | "template_with_drift" | "variable"
field_map: list[FieldMapping] | None = None  # coordinate-based field definitions
layout_confidence: float = 0.0

@dataclass
class FieldMapping:
    field_type: str          # "PERSON", "GOVERNMENT_ID", "DATE_OF_BIRTH", "LOCATION", etc.
    anchor_text: str         # text label to search for: "Client:", "Tax No", "Date of Birth"
    spatial_relationship: str  # "same_line_right", "line_below", "lines_below_N", "region_right"
    value_pattern: str | None  # optional regex for validation: r"\d{3}-\d{2}-\d{4}"
    sample_bbox: list[float]  # [x0, y0, x1, y1] from sample page (reference only)
    line_count: int = 1      # for multi-line fields like addresses
    skip_pattern: str | None = None  # text to skip (e.g., client code in parens)
```

**LLM prompt addition** (in UNDERSTAND_DOCUMENT / UNDERSTAND_MULTI_PAGE_DOCUMENT):

```
Analyze the layout structure of this document:

1. layout_type: Is every page formatted identically ("fixed"), does it follow
   a repeating template with slight variations ("template_with_drift"), or
   is the content freeform ("variable")?

2. If "fixed" or "template_with_drift", identify FIELD MAPPINGS:
   For each PII field visible, report:
   - field_type: what kind of data (PERSON, GOVERNMENT_ID, DATE_OF_BIRTH, LOCATION, etc.)
   - anchor_text: the text label that identifies this field (e.g., "Client:", "Tax No")
   - spatial_relationship: where is the value relative to the label?
     Options: "same_line_right", "line_below", "lines_below_4", "region_right"
   - value_pattern: regex pattern for the value (e.g., "\\d{3}-\\d{2}-\\d{4}" for SSN)
   - sample_bbox: approximate [x0, y0, x1, y1] coordinates of the value on this page
   - line_count: how many lines the value spans (1 for names, 4+ for addresses)
   - skip_pattern: text between label and value to skip (e.g., "(001968)" client code)

Example output:
{
  "layout_type": "fixed",
  "layout_confidence": 0.95,
  "field_map": [
    {
      "field_type": "PERSON",
      "anchor_text": "Client:",
      "spatial_relationship": "same_line_right",
      "value_pattern": null,
      "sample_bbox": [150, 120, 450, 140],
      "line_count": 1,
      "skip_pattern": "\\(\\d+\\)"
    },
    {
      "field_type": "GOVERNMENT_ID",
      "anchor_text": "Tax No",
      "spatial_relationship": "line_below",
      "value_pattern": "\\d{3}-\\d{2}-\\d{4}",
      "sample_bbox": [400, 85, 520, 100],
      "line_count": 1
    },
    {
      "field_type": "LOCATION",
      "anchor_text": "In Account with",
      "spatial_relationship": "lines_below_4",
      "sample_bbox": [300, 140, 550, 220],
      "line_count": 4,
      "skip_pattern": "\\(\\d+\\)\\s+[A-Z ]+"
    }
  ]
}
```

#### 21b. Coordinate Extractor — Python processes all pages

**File: `app/pipeline/coordinate_extractor.py`** (new):

Uses PyMuPDF (fitz) word-level bounding boxes. For each page: find anchor text label, define search region based on spatial_relationship, collect words in that region, validate against value_pattern.

```python
class CoordinateExtractor:
    """
    Fast extraction for fixed-layout documents.
    LLM provides the field map, Python extracts from every page using coordinates.
    Processes 1000+ pages in seconds.
    """
    
    def __init__(self, field_map: list[FieldMapping], doc_path: str, doc_id: str):
        self.field_map = field_map
        self.doc_path = doc_path
        self.doc_id = doc_id
    
    def extract_all_pages(self, page_range=None) -> tuple[list[PIIRecord], list[int]]:
        """Returns (records, failed_pages)."""
        doc = fitz.open(self.doc_path)
        records, failed_pages = [], []
        
        for page_num in (page_range or range(doc.page_count)):
            page = doc[page_num]
            words = page.get_text("words")
            rec = PIIRecord(record_id=str(uuid4()), source_document_id=self.doc_id,
                           page_range=str(page_num + 1), extraction_method="coordinate")
            
            success = True
            for field in self.field_map:
                value = self._extract_field(words, field, page)
                if value:
                    self._set_field(rec, field.field_type, value)
                elif field.field_type == "PERSON":
                    success = False
            
            if success and rec.raw_name:
                records.append(rec)
            else:
                failed_pages.append(page_num)
            doc._forget_page(page)
        
        doc.close()
        return records, failed_pages
    
    def _extract_field(self, words, field, page):
        """Find anchor text, extract value at relative position."""
        anchor_words = self._find_anchor(words, field.anchor_text)
        if not anchor_words:
            return None
        anchor_bbox = self._merge_bboxes(anchor_words)
        
        # Region based on spatial_relationship
        if field.spatial_relationship == "same_line_right":
            region = (anchor_bbox[2]+5, anchor_bbox[1]-5, page.rect.width-20, anchor_bbox[3]+5)
        elif field.spatial_relationship == "line_below":
            lh = (anchor_bbox[3]-anchor_bbox[1]) or 15
            region = (anchor_bbox[0]-50, anchor_bbox[3], page.rect.width-20, anchor_bbox[3]+lh*1.5)
        elif field.spatial_relationship.startswith("lines_below_"):
            n = int(field.spatial_relationship.split("_")[-1])
            lh = (anchor_bbox[3]-anchor_bbox[1]) or 15
            region = (anchor_bbox[0]-50, anchor_bbox[3], page.rect.width-20, anchor_bbox[3]+lh*n*1.5)
        else:
            region = (anchor_bbox[2]+5, anchor_bbox[1]-5, page.rect.width-20, anchor_bbox[3]+5)
        
        region_words = sorted([w for w in words if self._in_region(w, region)], key=lambda w: (w[1], w[0]))
        value = self._words_to_text(region_words, field.line_count)
        
        if field.skip_pattern and value:
            value = re.sub(field.skip_pattern, "", value).strip()
        if field.value_pattern and value:
            if not re.search(field.value_pattern, value):
                return None
        return value or None
```

#### 21c. Reconciliation — LLM handles failed pages

**File: `app/pipeline/reconciliation.py`** (new):

Pages where coordinate extraction failed (anchor not found, value didn't match pattern) are sent to LLM for direct extraction.

```python
class ExtractionReconciler:
    def reconcile(self, failed_pages, doc_path, doc_id, field_map, ollama_client):
        """Send failed pages to LLM. Returns recovered PIIRecords."""
        records, still_failed = [], []
        for page_num in failed_pages:
            page_text = self._get_page_text(doc_path, page_num)
            try:
                response = ollama_client.generate(
                    prompt=self._build_prompt(page_text, field_map),
                    system="You are a document data transcription assistant.",
                    use_case="reconciliation_extraction", document_id=doc_id)
                rec = self._parse_response(response, doc_id, page_num)
                if rec:
                    rec.extraction_method = "llm_reconciliation"
                    records.append(rec)
                else:
                    still_failed.append(page_num)
            except Exception:
                still_failed.append(page_num)
        
        logger.info(f"Reconciliation: {len(records)} recovered, {len(still_failed)} failed")
        return records
```

#### 21d. Auditor Field Map Editor — Frontend

When layout_type is "fixed" or "template_with_drift", the analysis review panel shows:

- Radio: Coordinate-based extraction (recommended) vs AI-assisted extraction
- Estimated time for each method
- Editable field mapping list: each mapping shows field name, anchor text, position, sample value
- Edit / Remove buttons per mapping
- "+ Add field mapping" button
- "Approve with mappings" / "Reject" buttons

**API endpoints:**

```
GET  /jobs/{id}/analysis    → includes field_map, layout_type, layout_confidence
PUT  /jobs/{id}/field-map   → auditor edits field mappings before approving
POST /jobs/{id}/approve     → saves approved field_map to DocumentSchema
```

#### 21e. Pipeline Integration

In `run_extraction_background()`, coordinate extraction is the FIRST check (before template/tabular/page paths):

```python
if schema.layout_type == "fixed" and schema.field_map:
    # COORDINATE PATH — fast (seconds)
    extractor = CoordinateExtractor(schema.field_map, doc.file_path, str(doc.id))
    records, failed_pages = extractor.extract_all_pages()
    if failed_pages:
        reconciler = ExtractionReconciler()
        recovered = reconciler.reconcile(failed_pages, doc.file_path, str(doc.id),
                                         schema.field_map, ollama_client)
        records.extend(recovered)

elif schema.template and schema.template.is_repeating:
    # TEMPLATE PATH (unchanged)
elif schema.is_tabular:
    # TABLE PATH (unchanged)
else:
    # PAGE PATH (unchanged)
```

#### 21f. Execution Prompts (split into 4 focused runs)

**Run 1: Layout assessment + FieldMapping model**

```
Read CLAUDE.md and docs/PLAN.md Step 21a for context.

1. Add to DocumentSchema in app/structure/document_schema.py:
   - layout_type: str = "variable"
   - field_map: list[FieldMapping] | None = None
   - layout_confidence: float = 0.0
   Create FieldMapping dataclass with all fields from Step 21a.

2. Update LLM document understanding prompts to ask for layout_type
   and field_map when layout is fixed/template_with_drift.

3. Update _parse_response() to parse layout_type, field_map.
   Defensive: if field_map parsing fails, default to layout_type="variable".

4. Tests: fixed layout doc → field_map populated, variable doc → field_map None.

Run pytest. Update CLAUDE.md.
```

**Run 2: Coordinate extractor + reconciliation**

```
Read CLAUDE.md and docs/PLAN.md Step 21b-c for context.

1. Create app/pipeline/coordinate_extractor.py:
   CoordinateExtractor class with extract_all_pages().
   Uses PyMuPDF word-level bounding boxes.
   Anchor-based: find label text, extract value at relative position.
   Returns (records, failed_pages).

2. Create app/pipeline/reconciliation.py:
   ExtractionReconciler class with reconcile().
   Sends failed pages to LLM for direct extraction.

3. Tests: mock page with labeled fields → correct extraction,
   missing anchor → page in failed_pages list,
   reconciliation → LLM called for failed pages.

Run pytest. Update CLAUDE.md.
```

**Run 3: Pipeline wiring**

```
Read CLAUDE.md and docs/PLAN.md Step 21e for context.

1. In run_extraction_background(), add coordinate path as FIRST check:
   if schema.layout_type == "fixed" and schema.field_map:
     use CoordinateExtractor → reconcile failures → continue to dedup

2. Existing paths unchanged (template, tabular, page, presidio).

3. In analyze_generator(), include field_map in the analysis response
   and extraction preview.

4. Tests: fixed layout → coordinate path taken,
   template layout → LLM path taken (unchanged).

Run pytest. Update CLAUDE.md.
```

**Run 4: Frontend field map editor**

```
Read CLAUDE.md and docs/PLAN.md Step 21d for context.

1. PUT /jobs/{id}/field-map endpoint in app/api/routes/analysis_review.py

2. Frontend AnalysisReviewPanel in ProjectDetail.tsx:
   - Show field mappings when layout_type is "fixed"
   - Each mapping: field name, anchor, position, sample value
   - Edit/Remove buttons per mapping
   - "Add field mapping" button
   - Radio: Coordinate vs AI extraction
   - Estimated time display

3. client.ts: updateFieldMap() API function.

Run pytest. Update CLAUDE.md.
```
---

### Step 23 — Hybrid Pipeline Integration & Multi-Format Orchestration (COMPLETE — NO REMAINING GAPS)

**Status: COMPLETE. Initial gap analysis found 3 features already present + 2 additional gaps that were then fixed.**

**Features already present (no changes needed):**

1. **Static Value Filtering** — `app/pipeline/static_filter.py` → `filter_static_values()`, wired into `two_phase.py` for Path 0 and Paths 1/2/3. Thresholds: PERSON >80%, other >50%, min 5 pages. Never filters US_SSN/GOVERNMENT_ID.

2. **Consistency Scoring / Audit** — `app/pipeline/extraction_verifier.py` → `verify_by_coordinates()`, median-based outlier detection, weighted scoring, PASS/REVIEW/FAIL thresholds. Wired into `two_phase.py` after ALL extraction paths.

**Gap 1 fix — Mixed-case name regex learning:**
- **Problem:** Structural name matcher only handled ALL_CAPS. Mixed-case names ("Smith, John", "John Smith") had no fallback when anchor extraction failed.
- **Fix:** `_learn_name_regex()` in `app/pipeline/coordinate_extractor.py` — detects 5 name formats from vision samples (last_first, titled, first_last, all_caps, generic) with Unicode support (José, García, Müller). Returns compiled regex. Used as second fallback in `extract_all_pages()` after structural matching, before address fallback.
- **Wiring:** `app/pipeline/two_phase.py` — person samples persisted to `doc.metadata_json["person_samples"]` during vision routing (analysis phase), loaded and passed as `name_samples=` to `CoordinateExtractor` during extraction.
- **Tests:** `TestNameRegexLearning` (12 tests), `TestNameRegexFallback` (5 tests), `TestPersonSamplesPersistence` (3 tests).

**Gap 2 fix — Archive extraction in folder-based discovery:**
- **Problem:** Archives (.zip/.7z) in `_KNOWN_EXTENSIONS` got discovered but couldn't be read — no reader registered, `TikaReader` raised `NotImplementedError`. Upload jobs handled this via `upload_helpers.extract_archive()`, but source_directory jobs failed.
- **Fix:** Two-pass `list_documents()` in `app/tasks/discovery.py` — Pass 1 extracts any archives to `<stem>_extracted/` subdirectories (reusing `upload_helpers.extract_archive()`). Pass 2 discovers all non-archive files including extracted contents. Idempotent (reuses existing `_extracted/` directories). Graceful failure on bad archives.
- **Tests:** `TestArchiveDiscovery` (7 tests — zip contents, regular files alongside, bad zip, unsupported filter, idempotent, nested, reuse).

**2787 tests passing (26 new). Pre-existing failures in test_pattern_validator, test_vision_extraction, test_vision_router, test_step23_hybrid unrelated.**

**Original proven metrics (March 2026, 34 real breach documents):**

#### What Was Proven (standalone testing — March 2026)

**Standalone scripts:** `test_hybrid_pipeline.py` (PDF engine), `forentis_extract.py` (47-format orchestrator)

**Test results across 34 documents:**

| Category | Docs | Records | Audit |
|---|---|---|---|
| Text PDFs (structured) | 23 | 77,410 | 17 PASS, 2 fallback |
| Scanned PDFs (OCR/Vision) | 5 | 15 | All via vision OCR |
| MSG emails (body extraction) | 5 | 49 | Body text PII |
| HEIC/JPG images (phone photos) | 2 | 2 | Vision for ID cards |
| XLS/XLSX spreadsheets | 2 | 66 | Tabular mapping |
| **TOTAL** | **34** | **78,471** | **33/34 working** |

**Key innovations proven:**

1. **Onset detection with cover page penalty** — cover pages score -20 per signal word ("Report Summary", "Account Criteria"), data pages score +50 per name/account/SSN. Solved AWIR-038 and TALX which previously picked cover pages.

2. **Structural name matcher for ALL_CAPS embedded names** — analyzes vision samples into structures like `(INITIAL, WORD, WORD)`, matches against page text. Complex1: 0→8,617 PERSON.

3. **Template-based extraction at ms/page** — vision analyzes ONE page, builds spatial template (proximity, same-line rules), Python applies to ALL pages. 4,200 pages in 157 seconds.

4. **Coordinate-based audit (no vision needed)** — verifies extracted values exist in source text, checks format validity, measures record count consistency. 17/17 PASS instantly.

5. **Text-based PERSON discovery** — when vision reports 0 PERSON, scans nearby pages for name patterns. Boosey: 0→1,427 PERSON.

6. **Three-tier scanned PDF handling** — Vision OCR (best) → Tesseract+regex (fallback) → graceful empty.

7. **Multi-format unified orchestration** — 47 file extensions, one command, automatic routing.

8. **Dual-model fallback** — qwen2.5vl primary, llama3.2-vision fallback. AWIR-038 and TALX extracted via fallback when primary threw 500 errors.

---

#### 23a. Wire Hybrid Extraction into `two_phase.py`

The existing pipeline in `two_phase.py` already has:
- `content_onset.py` — onset detection (needs cover page penalty from proven script)
- `vision_router.py` — vision analysis (working, add fallback model support)
- `field_map_builder.py` — vision→coordinates bridge (working)
- `coordinate_extractor.py` — coordinate extraction (needs proximity/embedded name logic)
- `extraction_verifier.py` — post-extraction audit (needs coordinate-based text verification)

**Integration plan (NOT a rewrite — enhance existing components):**

```
1. content_onset.py — port cover page penalty scoring from test_hybrid_pipeline.py
   Add: COVER_PAGE_SIGNALS list, -20/+50 scoring, diversity bonus
   
2. vision_router.py — add fallback_model parameter
   Add: try primary → on 500 → retry with fallback model
   
3. coordinate_extractor.py — port structural name matcher + proximity rules
   Add: _analyze_structure(), find_structural_names(), _clean_name()
   Add: embedded name detection (names inside same line as other data)
   
4. extraction_verifier.py — port coordinate-based text audit
   Replace vision-based audit with text verification (instant, deterministic)
   
5. two_phase.py — add scanned PDF path
   Before: scanned PDFs → skip
   After: scanned PDFs → vision OCR → regex extraction (or Tesseract fallback)
```

---

#### 23b. Multi-Format Reader Integration

Existing readers in `app/readers/`:
- `pdf_reader.py` ✅ 
- `docx_reader.py` ✅
- `excel_reader.py` ✅ (needs multi-tab join logic)
- `csv_reader.py` ✅
- `email_reader.py` ✅ (needs body text PII extraction)
- `html_reader.py` ✅
- `parquet_reader.py` ✅
- `ocr.py` ✅ (needs vision-first path for ID cards)

**New readers to add from `forentis_extract.py`:**
- `heic_reader.py` — HEIC→PNG conversion + vision/OCR
- `image_reader.py` — vision-first for JPG/PNG/WEBP + OCR fallback
- `sqlite_reader.py` — SQLite/DB files
- `dbf_reader.py` — dBase legacy files
- `mdb_reader.py` — Access databases via mdb-tools
- `vcf_reader.py` — vCard contact files
- `msg_reader.py` — enhanced MSG with body PII extraction
- `archive_reader.py` — ZIP/7z/RAR recursive extraction

**Enhancements to existing readers:**
- `excel_reader.py` — add multi-tab pattern detection (join/concat/independent)
- `email_reader.py` — extract PII from HTML body, not just attachments
- `ocr.py` — add 2x upscale for phone photos, vision-first path

---

#### 23c. End-to-End Workflow: Folder → Extract → Audit → Sample → Review → Notify

**This is the production workflow that ties everything together:**

```
┌─────────────────────────────────────────────────────────┐
│ STEP 1: DISCOVERY                                       │
│   Input: folder path (or upload via API)                │
│   Action: scan all files, classify by format            │
│   Output: file manifest with types + sizes              │
│   Code: tasks/discovery.py + readers/classifier.py      │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│ STEP 2: ROUTE TO READER                                 │
│   PDF text → hybrid pipeline (vision+template+coord)    │
│   PDF scanned → vision OCR → regex (3-tier)             │
│   XLSX/CSV → tabular extraction (multi-tab aware)       │
│   DOCX → table extraction → narrative fallback          │
│   MSG/EML → body PII + extract attachments → recurse    │
│   JPG/HEIC → vision-first → OCR fallback               │
│   ZIP/7z → extract → recurse on contents                │
│   Code: readers/*.py + pipeline/two_phase.py            │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│ STEP 3: PII EXTRACTION                                  │
│   For each file → extract PII records                   │
│   Each record: PERSON + fields (SSN, DOB, address...)   │
│   Each value: page#, char offset, bounding box          │
│   Code: pipeline/coordinate_extractor.py (fast path)    │
│         tasks/extraction.py (LLM path)                  │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│ STEP 4: COORDINATE-BASED AUDIT                          │
│   Sample N pages per document                           │
│   Verify: extracted values exist in source text          │
│   Check: format validity (SSN, DOB, email patterns)     │
│   Check: record count consistency across pages           │
│   Output: PASS (≥80%) / REVIEW (50-80%) / FAIL (<50%)  │
│   Code: pipeline/extraction_verifier.py                 │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│ STEP 5: NORMALIZATION + DEDUPLICATION                   │
│   Name normalization: "SMITH, JOHN" = "John Smith"      │
│   Address normalization: "PO BOX 74" = "P O BOX 74"    │
│   Cross-document dedup by SSN / name+DOB               │
│   Entity resolution → NotificationSubject records       │
│   Code: normalization/*.py + rra/*.py                   │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│ STEP 6: QC SAMPLING                                     │
│   Select 5-10% of AI-approved records for human review  │
│   Stratified: proportional across documents + PII types │
│   Create ReviewTask records in review queue             │
│   Code: review/sampling.py                              │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│ STEP 7: HUMAN-IN-THE-LOOP REVIEW                        │
│   AI_PENDING → HUMAN_REVIEW → APPROVED / REJECTED       │
│   Escalation path: → LEGAL_REVIEW for regulatory cases  │
│   Reviewer sees: original page image + extracted values  │
│   Can edit, approve, reject, escalate                    │
│   Code: review/workflow.py + review/queue_manager.py    │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│ STEP 8: NOTIFICATION                                    │
│   Build notification list from approved subjects         │
│   Apply regulatory protocol (HIPAA, state laws)         │
│   Generate: email template or print-ready postal letter │
│   Deliver: email sender or PDF for mailing              │
│   Code: notification/*.py + protocols/*.py              │
└─────────────────────────────────────────────────────────┘
```

**What exists vs what's missing:**

| Step | Component | Status |
|---|---|---|
| 1. Discovery | `tasks/discovery.py` | ✅ Exists |
| 2. Multi-format routing | `readers/*.py` | ✅ Most readers exist, need HEIC/image/MSG body/archive |
| 3. Hybrid extraction | `pipeline/coordinate_extractor.py` | ⚠ Exists but needs proven enhancements |
| 4. Coordinate audit | `pipeline/extraction_verifier.py` | ⚠ Exists but needs text-based verification |
| 5. Normalization + dedup | `normalization/*.py` + `rra/*.py` | ✅ Exists |
| 6. QC sampling | `review/sampling.py` | ✅ Exists |
| 7. HITL review | `review/workflow.py` | ✅ Exists |
| 8. Notification | `notification/*.py` | ✅ Exists |

---

#### 23d. Static Value Filter (Post-Extraction Cleanup)

**Problem proven in testing:** Report dates extracted as DOB, company phone numbers extracted as personal phones. The extraction is correct (value exists at the coordinates) but the value is static across all pages — it's metadata, not individual PII.

**Fix:** After extraction, identify values that appear on >50% of pages and remove them from individual records. These are report headers, not personal data.

```python
def filter_static_values(page_records, threshold=0.5):
    """Remove values that appear on too many pages (report metadata, not individual PII)."""
    value_counts = Counter()
    total_pages = len(page_records)
    for recs in page_records.values():
        page_vals = set()
        for r in recs:
            for v in r.values():
                page_vals.add(str(v))
        for v in page_vals:
            value_counts[v] += 1
    
    static = {v for v, c in value_counts.items() if c / total_pages > threshold}
    # Remove static values from records
    ...
```

---

#### 23e. Template Caching

**Problem:** Re-analyzing the same document layout burns vision model time. If the same layout/format appears in multiple documents (e.g., 50 AWIR files with identical structure), the template from the first should apply to all.

**Fix:** Hash the onset page text + layout → cache template. On subsequent documents with same hash → skip vision, reuse template.

---

### Step 24 — Pipeline Wiring & Upload Endpoint (COMPLETE)

**Status: COMPLETE.** Static filter + template cache wired into `two_phase.py`. File upload endpoint supports all 47 formats with archive/email extraction. Frontend shows coord audit results. 2787+ tests passing.

---

### Step 24e — Extraction Performance Fixes (COMPLETE)

**Context:** E2E test on 34 real documents (20,192 pages) revealed critical performance bottlenecks:
- 3733050.pdf (3063 pages): 32.9 hours via Path 2b (515 LLM batches, no field map)
- WashingtonCMD: 11.8 hours via Presidio + 50-page inline gap-fill
- Vision routing with model=None silently produced garbage routing for all docs

**4 fixes implemented:**

| Fix | Problem | Solution | Impact |
|---|---|---|---|
| **FIX 1**: Onset-aware field map validation | `_validate_field_map()` sampled pages [0, mid, end]; mid-page of 3063-page doc hits boundary → 0 records → coordinate path blocked | Two-strategy: (1) onset + 4 consecutive pages, require 2/5 valid names; (2) fallback to 3 random middle-third pages. Both must fail to reject. | Coordinate path no longer blocked by unlucky page sampling |
| **FIX 2**: Deferred gap-fill | Inline gap-fill ran 50 × 60s per doc DURING extraction loop | Moved to post-extraction stage with 50-call budget, priority-sorted (critical fields first), max 10 pages/doc | Extraction loop completes in minutes, gap-fill capped at ≈20 min total |
| **FIX 3**: LLM batch budget | Path 2b: no cap → 515 batches = 33 hours | Capped at 100 batches. Over-budget: learn-then-extract hybrid (LLM first 300 instances → learn name regex → code-extract remainder) | 3063-page doc: ≈10 min LLM + seconds for code extraction |
| **FIX 4**: VisionRouter no-model guard | Both models=None → silent garbage routing → all docs miss coordinate path | Early return with error log when no vision model configured | Prevents cascade failure across entire job |

**Files modified:** `app/pipeline/two_phase.py`, `app/pipeline/vision_router.py`, `tests/test_two_phase.py`

---
---

## Phase 6 — Production Readiness

**Goal:** Close the table-stakes gaps that prevent real deployment. No law firm or breach response team will adopt a tool that handles PII without authentication, can't show source documents, or doesn't track regulatory deadlines. These three steps make Forentis AI deployable.

Steps 1-24 COMPLETE (2787+ tests). Phase 5 delivered the extraction engine. Phase 6 wraps it for production use.

---

### Step 25 — Authentication, RBAC & Access Audit Logging

**Goal:** Every API call requires a verified identity. Every action is logged with who did it. The four existing roles (REVIEWER, LEGAL_REVIEWER, APPROVER, QC_SAMPLER) are enforced — not just defined.

**Why table-stakes:** Breach data is attorney-client privileged. Uncontrolled access is a deal-breaker for any firm's InfoSec review. Regulators also expect access controls in the breach response process itself.

---

#### 25a. Auth Backend — JWT + Local User Store

**Add local user management with hashed passwords and JWT tokens.**

| File | What to do |
|---|---|
| `app/db/models.py` | Add `User` model: id, email, hashed_password, display_name, role, is_active, created_at, last_login_at |
| `alembic/versions/` | Migration for `users` table |
| `app/core/auth.py` | NEW — `hash_password()`, `verify_password()`, `create_access_token()`, `decode_token()`. Use bcrypt + PyJWT. Token expiry configurable via settings. |
| `app/core/settings.py` | Add `jwt_secret_key`, `jwt_expiry_minutes` (default 480 = 8hr), `auth_enabled` (default true) |
| `app/api/routes/auth.py` | NEW — `POST /auth/login` (email+password → JWT), `POST /auth/register` (admin-only), `GET /auth/me`, `POST /auth/change-password` |
| `app/api/deps.py` | Add `get_current_user()` dependency — extracts JWT from `Authorization: Bearer` header, returns `User` or raises 401 |
| `tests/test_auth.py` | Login flow, token expiry, bad credentials, password hashing |

**Air-gap safe:** No OAuth, no external IdP. Local user store only. JWT secret generated on first boot and stored in settings.

---

#### 25b. RBAC Enforcement on All Routes

**Every route gets a role guard. Existing roles are enforced, not just defined.**

| Role | Can do |
|---|---|
| `QC_SAMPLER` | Read-only access to review queue, sampling results |
| `REVIEWER` | Above + approve/reject documents, review subjects |
| `LEGAL_REVIEWER` | Above + escalation decisions, regulatory protocol changes |
| `APPROVER` | Above + start extraction, export, send notifications, manage users |

| File | What to do |
|---|---|
| `app/core/auth.py` | Add `require_role(*roles)` dependency factory — checks `current_user.role in roles`, raises 403 if not |
| `app/api/routes/jobs.py` | Add `require_role("REVIEWER", "APPROVER")` to upload, extraction, cancel. `APPROVER` only for delete. |
| `app/api/routes/review.py` | Enforce `REVIEWER` minimum for approve/reject |
| `app/api/routes/exports.py` | Enforce `APPROVER` for export creation |
| `app/api/routes/analysis_review.py` | Enforce `REVIEWER` for approve/reject, `APPROVER` for approve-all |
| `tests/test_rbac.py` | Test each role against each endpoint — verify 403 for insufficient privileges |

---

#### 25c. Access Audit Logging

**Every authenticated action is logged to an append-only access log.**

| File | What to do |
|---|---|
| `app/db/models.py` | Add `AccessLog` model: id, user_id, action, resource_type, resource_id, ip_address, timestamp |
| `app/api/middleware/access_log.py` | NEW — middleware that logs every request with user identity, action, and resource |
| `app/api/routes/audit.py` | Add `GET /audit/access-log` — paginated, filterable by user/action/date range. APPROVER only. |
| `tests/test_access_log.py` | Verify log entries created for all sensitive actions |

**Rule:** Access log is append-only. No DELETE endpoint. No UPDATE. Rows are immutable.

---

### Step 26 — Source Document Viewer

**Goal:** An auditor can click on any extracted value and see the original source page with the extraction highlighted. Side-by-side: extracted data on the left, source page image on the right.

**Why table-stakes:** The core audit workflow is "verify extraction against source." Without this, auditors must manually open PDFs and navigate to page numbers. That's not a product — it's a pipeline with a CSV output.

---

#### 26a. Page Rendering API

**Serve individual PDF pages as images for the frontend viewer.**

| File | What to do |
|---|---|
| `app/api/routes/documents.py` | NEW — `GET /documents/{doc_id}/pages/{page_num}` returns PNG image of the page. `GET /documents/{doc_id}/pages/{page_num}/text` returns page text with word bounding boxes. |
| `app/pdf/renderer.py` | Already has `render_page_to_image()`. Expose via API. Add highlight overlay: given bounding boxes, draw semi-transparent rectangles on the rendered image. |
| `app/core/settings.py` | Add `page_render_dpi` (default 150), `page_render_max_width` (default 1200) |
| `tests/test_document_viewer.py` | Test page rendering, highlight overlay, page count, 404 for invalid pages |

**Security:** Page images are served through auth middleware. No unauthenticated access to source documents. Images are generated on-demand, never cached to disk (breach data shouldn't persist as images).

---

#### 26b. Frontend Document Viewer Component

**React component that shows source page alongside extraction results.**

| File | What to do |
|---|---|
| `frontend/src/components/DocumentViewer.tsx` | NEW — side-by-side panel. Left: extraction results (name, SSN, DOB with page/bbox references). Right: rendered page image with bounding box overlays. Page navigation (prev/next). Zoom controls. |
| `frontend/src/api/client.ts` | Add `getPageImage(docId, pageNum, highlights?)` and `getPageText(docId, pageNum)` API functions |
| `frontend/src/pages/ProjectDetail.tsx` | Add "View Source" button on each document card in AnalysisReviewPanel. Opens DocumentViewer in a slide-over panel. |
| `frontend/src/pages/SubjectDetail.tsx` | Add "View Source" link on each extracted value — opens DocumentViewer at the relevant page with the field highlighted |

---

#### 26c. Extraction-to-Source Linking

**Every extracted value must link back to its source location.**

| File | What to do |
|---|---|
| `app/api/routes/analysis_review.py` | Include page_range and bbox in extraction results returned to frontend |
| `app/api/routes/documents.py` | `GET /documents/{doc_id}/extractions` — return all extractions for a document with page/bbox references, grouped by page |
| `frontend/src/components/DocumentViewer.tsx` | Click an extraction → scroll to that page, highlight that bbox |

---

### Step 27 — Regulatory Deadline Dashboard

**Goal:** Every active breach has a countdown showing days remaining until notification deadline. The dashboard shows deadline status across all active matters. Morning briefing view for breach response teams.

**Why table-stakes:** The notification deadline is the single most important number in a breach response. HIPAA: 60 days. GDPR: 72 hours. Missing the deadline has regulatory consequences. Every tool in this space shows a countdown.

---

#### 27a. Breach Date Tracking

**Add breach discovery date to the data model. Compute deadlines from protocol.**

| File | What to do |
|---|---|
| `app/db/models.py` | Add to `IngestionRun`: `breach_discovered_at` (DateTime, nullable), `breach_occurred_at` (DateTime, nullable, for the actual incident date). Add to `Project`: `breach_discovered_at`, `breach_occurred_at` (inherited by runs). |
| `alembic/versions/` | Migration adding date columns |
| `app/api/routes/projects.py` | Accept `breach_discovered_at` and `breach_occurred_at` in project create/update |
| `app/api/routes/jobs.py` | Accept `breach_discovered_at` in job submission |
| `app/protocols/protocol.py` | Add `compute_deadline(discovery_date) → deadline_date` method. Add `days_remaining(discovery_date) → int` method. |
| `tests/test_deadlines.py` | Test deadline computation for each protocol. Edge cases: GDPR 72 hours (not days), weekends, already-expired. |

---

#### 27b. Deadline Dashboard API & Frontend

**Portfolio-level view of all active matters with deadline status.**

| File | What to do |
|---|---|
| `app/api/routes/dashboard.py` | Add `GET /dashboard/deadlines` — returns all active projects with: project name, protocol, breach_discovered_at, deadline_date, days_remaining, status (on_track / at_risk / overdue), total_subjects, notification_progress (sent/total). |
| `frontend/src/pages/Dashboard.tsx` | Add deadline widget: table of active matters sorted by urgency. Color coding: green (>14 days), amber (3-14 days), red (<3 days), black (overdue). Click → project detail. |
| `frontend/src/components/DeadlineCountdown.tsx` | NEW — reusable countdown component. Shows "23 days remaining" or "OVERDUE by 4 days". Used in Dashboard and ProjectDetail. |
| `frontend/src/pages/ProjectDetail.tsx` | Add deadline countdown at top of project view. Show breach date, deadline date, days remaining. |
| `tests/test_dashboard_deadlines.py` | Test deadline API, sorting, status computation |

---
---

## Phase 7 — Workflow Completeness

**Goal:** Complete the auditor workflow loop. Phase 6 makes the tool deployable. Phase 7 makes it a complete product — evidence packaging, notification preview, iterative re-extraction, and manual entity management.

---

### Step 28 — Evidence Package Export

**Goal:** One-click export of a complete evidence bundle: methodology report (PDF), notification list (XLSX), extraction audit trail, QC sampling results, and regulatory filing summary. This is what counsel attaches to the regulatory notification.

---

#### 28a. Methodology Report Generator

| File | What to do |
|---|---|
| `app/export/methodology_report.py` | NEW — generates a PDF report containing: engagement summary (project name, protocol, date range), document inventory (files processed, pages, formats), extraction methodology (paths used: coordinate/vision/LLM/Presidio, per-document breakdown), verification results (audit status, field rates, static filter actions), deduplication summary (records before/after, merge criteria), QC sampling results (sample size, pass rate), notification summary (subjects found, notification required count). |
| `app/export/evidence_bundle.py` | NEW — orchestrates: generate methodology PDF + notification list XLSX + audit trail CSV + QC sampling report → ZIP archive |
| `app/api/routes/exports.py` | Add `POST /exports/{job_id}/evidence-bundle` — triggers bundle generation, returns download URL |
| `frontend/src/pages/ProjectDetail.tsx` | Add "Export Evidence Bundle" button in the export section |
| `tests/test_evidence_bundle.py` | Test bundle generation with mock data, verify all expected files present |

---

#### 28b. XLSX Export with Multiple Sheets

| File | What to do |
|---|---|
| `app/export/xlsx_exporter.py` | NEW — multi-sheet XLSX: Sheet 1 "Notification List" (one row per subject), Sheet 2 "Extraction Detail" (one row per extracted value with source page), Sheet 3 "Document Inventory" (one row per file), Sheet 4 "Audit Trail" (decisions log). Use openpyxl. Auto-width columns, header formatting, freeze top row. |
| `app/api/routes/exports.py` | Add `format` parameter to export endpoint: `csv` (existing) or `xlsx` (new) |
| `tests/test_xlsx_export.py` | Test multi-sheet generation, column formatting, data integrity |

---

### Step 29 — Notification Preview & Batch Approval

**Goal:** Before sending 10,000 notification letters, the auditor previews exactly what recipients will receive. Merge fields rendered with real subject data. Batch approval with final sign-off.

---

#### 29a. Notification Preview API

| File | What to do |
|---|---|
| `app/api/routes/notifications.py` | NEW — `GET /notifications/{job_id}/preview/{subject_id}` renders the notification template with the subject's real data (masked for display). Returns HTML for email preview, PDF for letter preview. `GET /notifications/{job_id}/preview/sample` picks 3 random subjects and renders all 3. |
| `app/notification/template_renderer.py` | NEW — takes template + subject data → rendered HTML/PDF. Reuse existing `print_renderer.py` for PDF path, existing templates for email. |
| `tests/test_notification_preview.py` | Test template rendering with sample data, verify merge fields populated |

---

#### 29b. Batch Approval & Send

| File | What to do |
|---|---|
| `app/api/routes/notifications.py` | `POST /notifications/{job_id}/approve` — APPROVER role required. Sets notification list status to approved. `POST /notifications/{job_id}/send` — triggers email delivery + print generation for approved subjects. |
| `frontend/src/pages/ProjectDetail.tsx` | Add notification section: preview samples, approve batch, trigger send. Show send progress (sent/failed/total). |
| `app/audit/audit_log.py` | Log notification approval and send events with user identity |
| `tests/test_notification_batch.py` | Test approval flow, send triggering, audit logging |

---

### Step 30 — Per-Document Re-Extraction

**Goal:** An auditor can re-run extraction on a single document without re-running the entire job. Supports the iterative workflow: extract → review → adjust field map → re-extract → review again.

---

#### 30a. Single Document Re-Extraction API

| File | What to do |
|---|---|
| `app/api/routes/jobs.py` | Add `POST /jobs/{job_id}/extract/{doc_id}` — re-runs extraction for one document. Accepts optional field_map override. Returns SSE stream for that document only. |
| `app/pipeline/two_phase.py` | Extract the per-document extraction logic into `_extract_single_document()`. Currently inlined in the per-doc loop of `run_extraction_background()`. Make it callable independently. |
| `app/api/routes/analysis_review.py` | After field map update, offer "Re-extract with updated field map" action |
| `frontend/src/pages/ProjectDetail.tsx` | Add "Re-extract" button per document. Show progress inline. Replace old results with new. |
| `tests/test_reextraction.py` | Test single-doc re-extraction, field map override, result replacement |

---

### Step 31 — Manual Entity Merge/Split

**Goal:** An auditor can manually link or unlink notification subjects. "John Smith" on page 4 and "J. Smith" on page 87 might be the same person — or might not. The auditor decides, and the decision is logged.

---

#### 31a. Manual Merge/Split API

| File | What to do |
|---|---|
| `app/api/routes/subjects.py` | NEW — `POST /subjects/merge` (body: `{subject_ids: [id1, id2], rationale: "..."}`) — merges subjects into one, keeps the most complete record, logs decision. `POST /subjects/split` (body: `{subject_id: id, extraction_ids: [...], rationale: "..."}`) — splits one subject into two based on which extractions belong to which person. |
| `app/rra/deduplicator.py` | Add `merge_subjects(ids, rationale, actor)` and `split_subject(id, extraction_groups, rationale, actor)` methods. Update PersonEntity links. |
| `app/audit/audit_log.py` | Log merge/split events with before/after state |
| `frontend/src/pages/SubjectDetail.tsx` | Add "Merge with..." (search for other subject, confirm) and "Split" (select which extractions to separate) UI |
| `tests/test_manual_merge_split.py` | Test merge (2→1 subject, data preservation), split (1→2 subjects, extraction reassignment), audit trail |

---
---

## Phase 8 — Scale & Polish

**Goal:** Performance, multi-matter management, and quality-of-life improvements. Phase 6-7 make the tool deployable and complete. Phase 8 makes it efficient at scale.

---

### Step 32 — Orchestration & Parallel Processing

**Goal:** Replace the monolithic generator in `two_phase.py` with proper Prefect orchestration. Enable parallel document extraction, per-document failure recovery, and job scheduling.

| File | What to do |
|---|---|
| `app/pipeline/dag.py` | Implement `build_pipeline()` — wire actual Prefect tasks. Each document extraction is an independent Prefect task. Failures don't block other documents. |
| `app/pipeline/two_phase.py` | Extract per-document extraction into standalone Prefect tasks. Keep SSE polling relay. |
| `app/core/settings.py` | Add `max_parallel_extractions` (default 4), `extraction_timeout_minutes` (default 30) |
| `tests/test_orchestration.py` | Test parallel extraction, failure isolation, timeout handling |

---

### Step 33 — Multi-Matter Portfolio Dashboard

**Goal:** Firm-level view across all active breaches. "We have 6 active matters, 2 past deadline, 12,000 subjects total."

| File | What to do |
|---|---|
| `app/api/routes/dashboard.py` | Add `GET /dashboard/portfolio` — aggregate stats across all active projects: total projects, total subjects, total notifications sent/pending, deadline status breakdown |
| `frontend/src/pages/Dashboard.tsx` | Portfolio summary cards at top. Active matters list with status indicators. Charts: subjects by protocol, notification progress, timeline. |
| `tests/test_portfolio.py` | Test aggregation across multiple projects |

---

### Step 34 — Per-Project False Positive Deny List

**Goal:** Auditor says "ignore 'Washington' as PERSON in this project" and it persists across re-runs.

| File | What to do |
|---|---|
| `app/db/models.py` | Add `ProjectDenyList` model: id, project_id, entity_type, value, reason, created_by, created_at |
| `app/pii/presidio_engine.py` | Accept optional `deny_list` parameter. Before returning detections, filter out any that match the deny list. |
| `app/api/routes/projects.py` | `GET/POST/DELETE /projects/{id}/deny-list` — CRUD for deny list entries |
| `frontend/src/pages/ProjectDetail.tsx` | Add deny list management panel. "Add to deny list" action on any false positive in review. |
| `tests/test_deny_list_project.py` | Test deny list CRUD, filtering, persistence across re-runs |

---

### Step 35 — PDF Report Export

**Goal:** Export notification list and methodology as formatted PDF — not just CSV/XLSX. Required for regulatory filings.

| File | What to do |
|---|---|
| `app/export/pdf_exporter.py` | NEW — uses WeasyPrint (already a dependency for print_renderer). Generates formatted PDF: cover page, table of contents, notification list table, methodology section, statistics charts. |
| `app/api/routes/exports.py` | Add `format=pdf` option |
| `tests/test_pdf_export.py` | Test PDF generation, verify page count, content presence |