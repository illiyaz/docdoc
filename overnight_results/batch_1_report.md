# Batch 1: Simple PDFs — Overnight Extraction Report

**Date:** 2026-04-06
**Mode:** Regex-based extraction (no LLM/vision)
**Status:** SUCCESS (5/5 files processed)

---

## Summary Statistics

| Metric | Value |
|--------|-------|
| Files Processed | 5 |
| Success | 5 |
| Failed | 0 |
| Empty | 0 |
| Total Records Extracted | 332 |
| Total Pages | 10,706 |
| Average Speed | 0.15s per file |

---

## Per-File Breakdown

| Filename | Pages | Records | Time (s) | Rate (rec/sec) |
|----------|-------|---------|----------|---|
| 3046246.pdf | 158 | 18 | 0.16 | 112.5 |
| 3666752.pdf | 595 | 299 | 0.22 | 1,359 |
| 3733050.pdf | 3,063 | 2 | 0.11 | 18.2 |
| 3738594.pdf | 3,583 | 8 | 0.12 | 66.7 |
| 3738641.pdf | 3,307 | 5 | 0.13 | 38.5 |

---

## PII Field Types Found

```
PERSON:        299 (90.1%)
LOCATION:       63 (19.0%)
PHONE_NUMBER:    3 (0.9%)
```

**Note:** Records can contain multiple field types; percentages sum >100%.

---

## Data Quality Observations

### High-Confidence Records
- **3666752.pdf**: 299 records from 595 pages — consistent person name extraction (likely high-confidence dataset)
- **3046246.pdf**: 18 records from 158 pages — mix of persons and locations

### Low-Yield Files
- **3733050.pdf**: Only 2 records from 3,063 pages — suggests image-heavy or scanned format (regex ineffective)
- **3738594.pdf**: Only 8 records from 3,583 pages — similar low yield pattern
- **3738641.pdf**: Only 5 records from 3,307 pages — scanned/image document

### Potential False Positives / Org Metadata
- **City of Federal Way** appears frequently (sequential records 127-279) in location extraction
  - Likely municipality/jurisdiction footer or organizational identifier, not PII
  - Should review for context deny-list inclusion in production runs

---

## Errors & Edge Cases

None. All files processed without crashes or exceptions.

---

## Recommendations

1. **Large scanned PDFs**: 3733050, 3738594, 3738641 show very low extraction despite large page counts
   - Recommend: Run Vision routing or OCR analysis in future phases
   - Current regex strategy only effective on text-native PDFs

2. **Repeated "City of Federal Way"**: Context deny-list candidate
   - Add to `context_deny_list.py` to reduce false positives in similar datasets

3. **Performance**: Regex extraction runs at 0.12-0.22s per file (excellent)
   - Can scale to 1000+ files without bottleneck

---

## Next Steps

- Phase 6: Enable vision routing for low-yield scanned PDFs (3733050, 3738594, 3738641)
- Phase 7: Add organizational metadata patterns to deny-lists based on batch results
