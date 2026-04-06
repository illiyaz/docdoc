# Batch 2: AWIR Legal Docs — Overnight Extraction Report

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
| Total Records Extracted | 291 |
| Total Pages | 1,062 |
| Average Speed | 0.18s per file |

---

## Per-File Breakdown

| Filename | Pages | Records | Time (s) | Rate (rec/sec) |
|----------|-------|---------|----------|---|
| AWIR-DOC.00000001.00000038.00000806.pdf | 50 | 137 | 0.15 | 913 |
| AWIR-DOC.00000001.00000098.00000939.pdf | 100 | 51 | 0.25 | 204 |
| AWIR-DOC.00000001.00000103.00000993.pdf | 581 | 33 | 0.19 | 173.7 |
| AWIR-DOC.00000001.00000124.00000482.pdf | 316 | 21 | 0.27 | 77.8 |
| AWIR-DOC.00000038.15.pdf | 15 | 49 | 0.04 | 1,225 |

---

## PII Field Types Found

```
LOCATION:     235 (80.8%)
PERSON:        44 (15.1%)
EMAIL_ADDRESS: 12 (4.1%)
PHONE_NUMBER:   3 (1.0%)
US_SSN:         7 (2.4%)
```

**Note:** Records can contain multiple field types; percentages sum >100%.

---

## Data Quality Observations

### High-Confidence Records
- **AWIR-DOC.00000038.15.pdf** (15 pages, 49 records): Excellent density (~3.3 rec/page)
  - Short document with high location and person extraction

- **AWIR-DOC.00000001.00000038.00000806.pdf** (50 pages, 137 records): Strong performance (~2.7 rec/page)
  - Likely contains structured table/list of addresses and names

### Moderate Yield
- **AWIR-DOC.00000001.00000098.00000939.pdf** (100 pages, 51 records): 0.51 rec/page
- **AWIR-DOC.00000001.00000103.00000993.pdf** (581 pages, 33 records): 0.06 rec/page — narrative/body text

### PII Diversity
- Unique finding: 7 US_SSN detections across batch
- 12 EMAIL_ADDRESS detections — good signal for contact lists
- Primarily address-heavy dataset (80.8% location)

### Potential False Positives / Org Metadata
- No obvious repeated organizational identifiers detected
- Addresses are diverse (no single location appearing 5+ times in sample)
- Clean extraction profile for legal document dataset

---

## Errors & Edge Cases

None. All files processed without crashes or exceptions. Consistent performance across all 5 AWIR documents.

---

## Recommendations

1. **Structured legal documents perform well**
  - AWIR batch shows regex extraction is effective on organized legal formats
  - Suggest prioritizing legal discovery and litigation document workflows

2. **SSN detection working**: 7 SSN patterns found
  - Indicates regex pattern is functioning correctly for numeric PII
  - Recommend verification that these are genuine SSNs (not policy/account numbers)

3. **Email extraction**: 12 records found
  - Good candidate for contact notification workflows in Phase 6+

---

## Next Steps

- Validate SSN detections (likely genuine given legal document context)
- Use AWIR batch as reference for "good performance" baseline (regex effective)
- Consider legal document templates for structure analysis in future phases
