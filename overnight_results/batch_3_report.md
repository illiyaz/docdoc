# Batch 3: Corporate Docs — Overnight Extraction Report

**Date:** 2026-04-06
**Mode:** Regex-based extraction (no LLM/vision)
**Status:** PARTIAL (4/5 files extracted; 1 empty)

---

## Summary Statistics

| Metric | Value |
|--------|-------|
| Files Processed | 5 |
| Success | 4 |
| Empty | 1 |
| Failed | 0 |
| Total Records Extracted | 505 |
| Total Pages | 6,558 |
| Average Speed (successful) | 0.31s per file |

---

## Per-File Breakdown

| Filename | Pages | Records | Time (s) | Rate (rec/sec) | Status |
|----------|-------|---------|----------|---|--------|
| ABCNY_560_0001384129.pdf | 67 | 89 | 0.45 | 197.8 | ✓ |
| CMG_Inc_0000414153.pdf | 1,354 | 77 | 0.11 | 700 | ✓ |
| CMG_Inc_0001352703.pdf | 453 | 21 | 0.16 | 131.3 | ✓ |
| Complex1.pdf | 4,200 | 318 | 0.56 | 567.9 | ✓ |
| EdmondsSD_0003650859.pdf | 484 | 0 | 0.42 | 0 | ⚠ EMPTY |

---

## PII Field Types Found

```
US_SSN:         172 (34.1%)
EMAIL_ADDRESS:  133 (26.3%)
LOCATION:       204 (40.4%)
PERSON:          64 (12.7%)
PHONE_NUMBER:   122 (24.2%)
```

**Note:** Records can contain multiple field types; percentages sum >100%.

---

## Data Quality Observations

### High-Confidence Records
- **Complex1.pdf** (4,200 pages, 318 records): Best performer
  - Highest absolute record count despite large page count (0.076 rec/page density)
  - Likely contains mixed content (forms, lists, narratives)
  - SSN and email extraction working effectively

- **ABCNY_560_0001384129.pdf** (67 pages, 89 records): Excellent density (~1.33 rec/page)
  - Structured corporate record with strong person/location/contact info

### Strong PII Diversity
- **US_SSN**: 172 detections (34.1%) — highest in any batch
  - Suggests corporate benefit/payroll documents or tax records
  - Recommend validation: are these genuine SSNs or account numbers?

- **EMAIL_ADDRESS**: 133 detections (26.3%)
  - Strong signal for corporate contact lists and email directories
  - Good data for notification workflows

- **PHONE_NUMBER**: 122 detections (24.2%)
  - High phone extraction suggests employee directories or vendor lists

### Low Yield / Empty
- **EdmondsSD_0003650859.pdf** (484 pages, 0 records): Complete empty
  - Likely scanned images or unstructured narrative (no regex matches)
  - Candidate for vision routing in Phase 6

- **CMG_Inc_0001352703.pdf** (453 pages, 21 records): Low yield (0.046 rec/page)
  - Mixed content with sparse PII pattern matches

### Potential False Positives / Org Metadata
- **"Other Transactions"** detected as PERSON name
  - Likely financial transaction category header, not genuine person name
  - Add to context deny-list to reduce false positives

- No repeated organizational identifiers detected in sample
- Address diversity is good (no single location repeated 5+ times)

---

## Errors & Edge Cases

- **EdmondsSD_0003650859.pdf**: No extraction errors, but produced zero records
  - PDF is intact (0.42s processing time = normal), so not corrupted
  - Regex patterns did not match any content → likely scanned/image-based

---

## Recommendations

1. **SSN Validation**: Verify 172 SSN detections in batch 3
  - High count suggests genuine employee/benefit records (good signal)
  - Recommend manual spot-check of Complex1.pdf to confirm accuracy

2. **Email/Phone extraction**: Excellent performance
  - 133 emails + 122 phones = strong notification candidate data
  - Recommend prioritizing batch 3 for Phase 6 notification workflows

3. **Scanned document handling**: EdmondsSD_0003650859.pdf shows regex limitation
  - Recommend Phase 6 vision routing for corporate scanned documents
  - Will likely unlock significant additional records

4. **False positive review**:
  - Add "Other Transactions" to corporate deny-lists
  - Review Complex1.pdf sample for pattern effectiveness

---

## Performance Comparison vs Other Batches

| Batch | Total Records | Total Pages | Avg Records/Page | Avg Time |
|-------|---------------|-------------|------------------|----------|
| 1 (Simple PDFs) | 332 | 10,706 | 0.031 | 0.15s |
| 2 (AWIR Legal) | 291 | 1,062 | 0.274 | 0.18s |
| **3 (Corporate)** | **505** | **6,558** | **0.077** | **0.31s** |

**Batch 3 Performance Notes:**
- Highest absolute record count (505)
- Most diverse PII types (5 categories vs 3-4 in other batches)
- Most computationally expensive (0.31s avg)
- Best candidate for multi-format processing (complex content mix)

---

## Next Steps

- Validate batch 3 SSN detections (spot-check Complex1.pdf)
- Implement vision routing for scanned documents (EdmondsSD file)
- Add "Other Transactions" to corporate deny-lists
- Prioritize batch 3 for Phase 6 notification preview testing
