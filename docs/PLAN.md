# Implementation Plan — Forentis AI (Active Steps)

Active implementation steps (21+). For completed steps (Phases 1-4, Steps 1-20), see [PLAN_COMPLETED.md](PLAN_COMPLETED.md).
See [CLAUDE.md](../CLAUDE.md) for project overview and conventions.

**Phase 5 — Forentis AI Evolution (IN PROGRESS)**

Steps 1-20 COMPLETE (2185+ tests). See PLAN_COMPLETED.md for full details.

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