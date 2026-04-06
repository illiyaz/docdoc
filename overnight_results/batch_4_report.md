# Batch 4 Report: TPHS + Vamp PDFs

## Summary
- **Files Processed:** 5 PDFs
- **Success Rate:** 5/5 (100%)
- **Total Records Extracted:** 1,083
- **Total Execution Time:** 10.68 seconds

## Per-File Breakdown

| File | Pages | Records | Mode | Time | Notes |
|------|-------|---------|------|------|-------|
| TPHS2_656_0000067171.pdf | 902 | 173 | quick_regex | 0.42s | Large healthcare document |
| TPHS2_656_0000070715.pdf | 132 | 800 | quick_regex | 0.37s | Dense SSN extraction |
| TPHS2_656_0000071913.pdf | 211 | 71 | quick_regex | 0.25s | Standard structured text |
| Vamp0000068240.pdf | 4 | 33 | ocr_regex | 9.63s | Scanned PDF, OCR required |
| Vamp0000068247.pdf | 1 | 6 | quick_regex | 0.01s | Minimal content |

## PII Field Types Found

| Field Type | Count | Prevalence |
|------------|-------|------------|
| PERSON | 965 | 89.1% |
| LOCATION | 556 | 51.3% |
| US_SSN | 800 | 73.9% |
| PHONE_NUMBER | 3 | 0.3% |
| EMAIL_ADDRESS | 1 | 0.1% |

## Extraction Patterns & Notes

### US_SSN Dominance
- 800 SSNs extracted across 1,083 records indicates healthcare/employee datasets
- Primarily from TPHS2_656_0000070715.pdf (likely a personnel or benefits database)

### Person Records
- High PERSON extraction (965) with diverse name formats
- Examples: "ADAMS, BRADLEY JAY", "AIKMAN, MELISSA A", "AKINBAMIDELE, OPEYEMI"
- Mostly professional formatting (LAST NAME, FIRST NAME style)

### Location Data
- 556 location records include full addresses
- Examples: "1060 FIELDING PARK CT", "2547 MICKLE AVENUE", "5548 WHITTY LANE"
- Appears to be associated with employee/member records

### OCR Processing
- Vamp0000068240.pdf identified as scanned (4 pages, 8,298 OCR chars)
- Successfully processed despite image-only content
- 33 records extracted from limited OCR text

## Potential False Positives

### Repeated Values
- Many records show only LOCATION (e.g., "234 BAYWOOD DRIVE") without associated person
- Could indicate organizational or mailing address lists mixed with individual records
- Recommend manual review of LOCATION-only records for dedup/org address filtering

### Metadata Patterns
- PERSON field often paired with LOCATION (strong signal for real records)
- LOCATION alone may indicate template headers or organizational fields

## No Errors or Failures

All files processed successfully. No retry required.

## Recommendations

1. Review LOCATION-only records (556 standalone entries) for org address metadata
2. Validate SSN accuracy—high concentration suggests sensitive HR/benefits data
3. Batch 4 is production-ready; suitable for notification workflow
