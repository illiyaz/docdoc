# Batch 6 Report: Mixed Format Files

## Summary
- **Files Processed:** 5 files (HEIC, JPG, XLS, XLSX, PDF)
- **Success Rate:** 3/5 (60%)
- **Empty/Failed:** 1 empty, 1 failed
- **Total Records Extracted:** 154
- **Total Execution Time:** 6.91 seconds

## Per-File Breakdown

| File | Format | Records | Status | Method | Time | Notes |
|-------|--------|---------|--------|--------|------|-------|
| Vamp0000068253.heic | HEIC | 0 | FAILED | N/A | 0.0s | pillow-heif not installed |
| Vamp0000068254.jpg | JPG | 0 | EMPTY | tesseract_ocr | 6.7s | OCR returned 159 chars, no PII detected |
| Vamp0000068722.xls | XLS | 4 | SUCCESS | tabular | 0.01s | Account numbers only |
| Vamp0000069297.xlsx | XLSX | 62 | SUCCESS | tabular | 0.01s | Employee form data |
| WashingtonCMD_0000102080.pdf | PDF | 88 | SUCCESS | quick_regex | 0.2s | Organization addresses |

## PII Field Types Found

| Field Type | Count | Prevalence |
|------------|-------|------------|
| PERSON | 62 | 40.3% |
| LOCATION | 88 | 57.1% |
| EMAIL_ADDRESS | 3 | 1.9% |
| ACCOUNT_NUMBER | 4 | 2.6% |

## Detailed File Analysis

### 1. Vamp0000068253.heic — FAILED

**Status:** Dependency Missing

```
Error: pillow-heif not installed — pip install pillow-heif
```

- Image file (1.5 MB)
- Requires HEIC/HEIF codec library
- Recommendation: Install `pillow-heif` and re-run, or skip if not critical

### 2. Vamp0000068254.jpg — EMPTY

**Status:** OCR Completed, No PII Detected

- File: 282 KB image
- OCR method: Tesseract
- Characters recognized: 159 (very sparse)
- PII extracted: 0 records
- Likely a low-resolution or non-text image (chart, form scan without OCR-friendly quality)
- No error; correctly identified as non-extractable

### 3. Vamp0000068722.xls — SUCCESS

**Status:** Account Numbers Detected

- Old Excel format (207 KB)
- Records: 4 ACCOUNT_NUMBER entities
- Values: "2126209" repeated 4 times
- Assessment: Likely a single account number appearing in multiple rows (false positive dedup candidate)
- Recommendation: Review for org account vs. breach subject account

### 4. Vamp0000069297.xlsx — SUCCESS

**Status:** Employee Form Extraction

- Modern Excel format (19 KB)
- Records: 62 PERSON + 3 EMAIL_ADDRESS
- Sheet: "Sheet1" (employee form template)
- Column mapping detected:
  - PERSON → "Employee Name:____________________"
  - EMAIL_ADDRESS → "Personal Email:__________________________"

**Data Quality:**
- Many records are placeholder blanks ("____", "____", "____")
- Real field labels extracted (e.g., "Hire Date", "Job Title")
- Boilerplate instructions captured (e.g., "SEND FORMS AFTER ASSESSMENT TEST")

**Potential False Positives:**
- ~45 of 62 PERSON records are blank fill-in fields or form instructions
- Only ~17 actual employee names/identifiable data
- Heavily padded with form structure and template text

### 5. WashingtonCMD_0000102080.pdf — SUCCESS

**Status:** Organization Address List

- Large PDF (3.4 MB, 594 pages)
- Records: 88 LOCATION entities
- Extraction method: quick_regex (no OCR)
- Sample addresses:
  - "100 West Washington Street"
  - "1070 Marshall Street"
  - "64\nUnited Way"
  - "1302 Hamilton Blvd"

**Assessment:**
- Appears to be organizational facility or branch location list
- No PERSON names extracted
- Consistent address formatting suggests inventory or directory document
- Likely a false positive batch for notification (org metadata, not individual breach subjects)

## Extraction Quality Summary

### Real Records (High Confidence)
- **None clearly high-confidence** — this batch is heavily padded with org data/forms/blank fields

### Medium Confidence
- 4 account numbers (need org vs. subject validation)
- 3 email addresses from employee form (possibly real)
- Some Washington CMD addresses may be real locations

### False Positives (High Likelihood)
- ~45 blank form fields from XLSX (100% false positive)
- All 88 locations from WashingtonCMD (org address list, not individual records)
- 4 repeated account numbers (likely single org account, not breach subjects)

## Errors & Failures

### 1 Failure: HEIC Codec Missing
```
Vamp0000068253.heic: pillow-heif not installed
```
- **Action:** Install optional dependency
- **Command:** `pip install pillow-heif`
- **Priority:** Low — HEIC support is optional

### 1 Empty Result: JPG Image
- **Status:** Expected — low-quality image OCR
- **No error:** Correctly handled
- **Priority:** Review image quality; no action needed if skipping is acceptable

## Recommendations

1. **Batch 6 data quality:** This batch contains mostly organizational metadata, not individual breach subjects
2. **HEIC support:** Install pillow-heif if HEIC images are required for future batches
3. **XLSX template handling:** Filter out form template fields (blanks, instructions) before notification
4. **Address dedup:** Washington CMD location list should be filtered as org address, not individual breach subject
5. **Account number review:** The 4 ACCOUNT_NUMBER records need validation—likely false positive (single org account)

## Production Readiness

**Current Assessment:** NOT RECOMMENDED FOR NOTIFICATION without manual review

- Too many false positives (org addresses, blank form fields)
- Only 3-4 potentially real breach subjects (emails from form)
- Recommend: Manual review + org-filter application before notification workflow

**Future batches:** Ensure org address deny-lists and form-template filters are applied before extraction
