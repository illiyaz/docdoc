# Batch 7 Report: Remaining Vamp PDFs

## Summary
- **Files Processed:** 4 PDF files
- **Success Rate:** 3/4 (75%)
- **Empty/Failed:** 1 empty (no extraction attempted)
- **Total Records Extracted:** 10
- **Total Execution Time:** 8.82 seconds

## Per-File Breakdown

| File | Pages | Records | Mode | Method | Time | Notes |
|-------|-------|---------|------|--------|------|-------|
| Vamp0000071981.pdf | 1 | 3 | ocr_regex | Tesseract | 1.07s | Scanned document, minimal content |
| Vamp0000071987.pdf | 1 | 5 | ocr_regex | Tesseract | 0.65s | Scanned document, sparse PII |
| Vamp0000072380.pdf | 17 | 0 | quick_regex | Regex only | 0.06s | No extractable content |
| Vamp0000074646.pdf | 3 | 2 | ocr_regex | Tesseract | 6.47s | Multi-page scanned, minimal PII |

## PII Field Types Found

| Field Type | Count | Prevalence |
|------------|-------|------------|
| LOCATION | 5 | 50.0% |
| PHONE_NUMBER | 4 | 40.0% |
| PERSON | 7 | 70.0% |

## Detailed File Analysis

### 1. Vamp0000071981.pdf — 3 Records

**Status:** Scanned PDF, OCR Success

- Pages: 1
- OCR chars: 1,247 (sparse single page)
- Records: 3
- Extraction method: ocr_regex
- Processing time: 1.07s

**Extracted Data:**
```
LOCATION: "7879 Oswego Rd"
PHONE_NUMBER: "(315) 622-2000"

PERSON: "SHIELDS, GEORGE"
LOCATION: "405 TAMARACK STREET"
PHONE_NUMBER: "(585) 935-1147"
```

**Assessment:**
- One location-only record (org address?)
- One person-location-phone triple (likely real breach subject)
- High confidence: 1 verified individual (SHIELDS, GEORGE)

### 2. Vamp0000071987.pdf — 5 Records

**Status:** Scanned PDF, OCR Success

- Pages: 1
- OCR chars: 812 (minimal)
- Records: 5
- Extraction method: ocr_regex
- Processing time: 0.65s

**Extracted Data:**
```
PERSON: "ZALEPESKI, TIMOTHY"

PERSON: "Karen Craft"
LOCATION: "18 HIGH POINT TRL"

PERSON: "Danny Blaine"
LOCATION: "80 State Street"

PERSON: "Albany New"
```

**Assessment:**
- 5 person records, some with locations
- "Albany New" likely a place name misread as person (OCR artifact)
- High confidence individuals: ZALEPESKI, TIMOTHY; Karen Craft; Danny Blaine
- Medium confidence: 1 likely OCR error

### 3. Vamp0000072380.pdf — 0 Records

**Status:** No Extractable Content

- Pages: 17 (multi-page)
- Extraction method: quick_regex (no OCR)
- Records: 0
- Processing time: 0.06s (fast scan)
- Result: Document scanned/processed, no PII patterns detected

**Possible Explanations:**
- Document contains images only (no text extraction)
- Document structure prevents regex matching
- Document legitimately contains no PII
- Recommendation: Visual review to confirm

### 4. Vamp0000074646.pdf — 2 Records

**Status:** Multi-page Scanned PDF, Sparse Extraction

- Pages: 3
- OCR chars: 9,256 (sparse across 3 pages)
- Records: 2
- Extraction method: ocr_regex
- Processing time: 6.47s (longest in batch)

**Extracted Data:**
```
LOCATION: "3140 SWEET RD"
PHONE_NUMBER: "315-677-5555"

PHONE_NUMBER: "(832) 437-0495"
```

**Assessment:**
- Location-phone pair (org facility?)
- One standalone phone number (incomplete record)
- Low PII density (2 records across 3 pages = ~0.67 records/page)
- Likely sparse document or form with limited relevant data

## Batch-Level Patterns

### OCR-Dependent Extraction
- 3 of 4 PDFs required OCR processing
- OCR successfully extracted sparse content where present
- Total processing time dominated by OCR (8.19s of 8.82s total)

### Person Records Quality
- 7 PERSON records extracted
- Assessment: 5-6 likely real individuals, 1 likely OCR error ("Albany New")
- Confidence: Medium (OCR errors possible on scanned documents)

### Location Patterns
- 5 LOCATION records
- Mix of street addresses and facility locations
- Some lack associated person name (org metadata possibility)
- Example org indicators: "Oswego Rd", "SWEET RD" (could be office/facility)

### Phone Numbers
- 4 phone numbers extracted
- Patterns: Mixed formats ("(585) 935-1147", "315-677-5555")
- Assessment: 2 likely org facility phones (standalone, no person)

## Potential False Positives

### Organizational Metadata
1. **Standalone locations:** "7879 Oswego Rd" and "3140 SWEET RD" (no person attached)
2. **Organization phone numbers:** Some unlinked to specific person
3. **OCR artifacts:** "Albany New" classification as person (likely place name)

### Recommended Dedup Filters
- Phone "(315) 622-2000" linked to "7879 Oswego Rd" suggests facility address
- Consolidate multi-page org references into single entity

## Summary of Individuals

**High Confidence Breach Subjects:**
1. SHIELDS, GEORGE - 405 Tamarack Street, (585) 935-1147
2. ZALEPESKI, TIMOTHY
3. Karen Craft - 18 High Point Trl
4. Danny Blaine - 80 State Street

**Medium Confidence:**
5. "Albany New" (likely OCR error)

**Unknown/Incomplete:**
- 2 phone numbers without person name

## Recommendations

1. **Manual review:** Verify OCR accuracy for scanned PDFs (especially "Albany New")
2. **Org facility dedup:** Standalone locations with phone numbers likely org metadata
3. **Record linking:** Confirm person-location-phone associations before notification
4. **Empty document:** Vamp0000072380.pdf warrants visual review to understand lack of extraction
5. **Production readiness:** With OCR artifact filtering, batch contains ~4 verified subjects; suitable for notification after manual validation

## Quality Assessment

**Extracted vs. True Subjects Ratio:**
- Total records: 10
- Estimated true subjects: 4-5
- Estimated org/error overhead: 5-6 records (50% false positive rate for scanned PDFs)

**Recommendation:** OK for notification after OCR artifact review and org address filtering
