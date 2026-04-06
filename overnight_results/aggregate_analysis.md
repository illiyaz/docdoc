# Aggregate Analysis — Forentis AI Overnight Pipeline

**Date:** 2026-04-06
**Total files:** 34 | **Success:** 33/34 (97%) | **Total records:** 2,424

---

## 1. Results by Batch

| Batch | Label | Files | Success | Records | Avg Records/File |
|-------|-------|-------|---------|---------|-----------------|
| 1 | Simple PDFs | 5 | 5/5 | 332 | 66.4 |
| 2 | AWIR legal docs | 5 | 5/5 | 291 | 58.2 |
| 3 | Corporate docs | 5 | 5/5 | 505 | 101.0 |
| 4 | TPHS + Vamp PDFs | 5 | 5/5 | 1,083 | 216.6 |
| 5 | MSG email files | 5 | 5/5 | 49 | 9.8 |
| 6 | Mixed formats | 5 | 4/5 | 154 | 30.8 |
| 7 | Remaining Vamp PDFs | 4 | 4/4 | 10 | 2.5 |

## 2. File Type Performance

| Extension | Files | Records | Errors | Avg Records/File | Assessment |
|-----------|-------|---------|--------|-----------------|------------|
| .pdf | 25 | 2,309 | 0 | 92.4 | Excellent — primary format |
| .xlsx | 1 | 62 | 0 | 62.0 | Good — tabular extraction works |
| .msg | 5 | 49 | 0 | 9.8 | Fair — body text only, attachments skipped |
| .xls | 1 | 4 | 0 | 4.0 | Fair — legacy format, limited header mapping |
| .heic | 1 | 0 | 1 | 0 | Failed — missing pillow-heif dependency |
| .jpg | 1 | 0 | 0 | 0 | Empty — OCR found no PII patterns |

## 3. Extraction Mode Distribution

| Mode | Files | Notes |
|------|-------|-------|
| quick_regex | 21 | Text PDF fast path — bulk of extractions |
| email_body | 5 | MSG body text parsing |
| ocr_regex | 4 | Tesseract OCR on scanned PDFs |
| unknown | 4 | Non-PDF formats (XLS, XLSX, JPG, HEIC) |

## 4. False Positive Analysis

### High-Frequency Values (Likely Organizational Metadata)

These values appear repeatedly and are almost certainly NOT breach subjects:

**PERSON field:**
- `____` (52 occurrences) — blank placeholder, OCR artifact
- `Ann Maynard` (5x) — email sender, not a breach subject
- `Johnstone Supply` (5x) — company name misclassified as person
- `Group Rochester` (5x) — org name fragment
- `Drug Test` (3x) — label misclassified as person

**PHONE_NUMBER field:**
- `(585) 482-8000` (5x) — likely organization main phone number

**EMAIL_ADDRESS field:**
- `amaynard@jswallacegroup.com` (5x) — sender email, not subject PII
- `support@etbservices.com` (5x) — generic support email

### False Positive Categories Identified

1. **Organizational names as PERSON:** Company names (Johnstone Supply, Group Rochester) matching the name regex. Needs entity-type disambiguation.

2. **Sender/CC metadata as subject PII:** Email sender names and addresses appearing in every MSG extraction. Need sender-role filtering.

3. **Blank/placeholder values:** `____` appearing 52 times from OCR artifacts. Need minimum content validation.

4. **Repeated organizational contact info:** Same phone/email appearing across many records, indicating org metadata rather than individual PII.

5. **Label misclassification:** Terms like "Drug Test" being captured as PERSON due to capitalization pattern.

## 5. Zero-Record Files (Need Vision/LLM)

| File | Pages | Issue |
|------|-------|-------|
| EdmondsSD_0003650859.pdf | 484 | Likely scanned — no text layer |
| Vamp0000068254.jpg | 1 | Image — OCR found no PII patterns |
| Vamp0000072380.pdf | 17 | Text PDF but no regex-matchable PII |

## 6. Performance Analysis

- **Fastest:** MSG emails at ~0.01s each
- **Slowest:** OCR PDFs at 1-10s per file (Tesseract rendering)
- **Best density:** TPHS2_656_0000070715.pdf — 800 records from 132 pages (6.1 recs/page)
- **Worst density:** Large scanned PDFs (3733050, 3738594, 3738641) — 2-8 records from 3000+ pages

## 7. Recommendations

1. **Add frequency-based value suppression:** Values appearing on >80% of pages in a document should be flagged as organizational metadata and suppressed from notification lists.

2. **Add sender-role filtering for emails:** Exclude sender name/email from PII extraction results. Only extract PII from body content referring to third parties.

3. **Add minimum-content validation for PERSON:** Reject values that are blank, single character, or match common labels/placeholders.

4. **Install pillow-heif for HEIC support:** Single missing dependency caused batch 6's only failure.

5. **Vision routing for scanned PDFs:** 3 zero-record files and several low-yield files would benefit from vision model analysis instead of regex fallback.
