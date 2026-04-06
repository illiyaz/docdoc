# Forentis AI Overnight Extraction — Batches 4-7

## Quick Reference

Run on: April 6, 2026
Mode: Regex-only extraction (no LLM, no vision)
Total time: ~27 seconds
Total files: 18
Total records: 1,296

## Results at a Glance

**Batch 4: TPHS + Vamp PDFs**
- Status: ✓ PRODUCTION-READY
- Files: 5 (100% success)
- Records: 1,083
- Quality: HIGH
- Action: Deploy to notification

**Batch 5: MSG Emails**
- Status: ⚠ NEEDS FILTERING
- Files: 5 (100% success)
- Records: 49 (88% overhead)
- Quality: LOW
- Action: Apply org deny-list, re-extract

**Batch 6: Mixed Formats**
- Status: ✗ NOT RECOMMENDED
- Files: 5 (60% success, 1 failed, 1 empty)
- Records: 154 (97% false positives)
- Quality: VERY LOW
- Action: Skip or manual review only

**Batch 7: Scanned PDFs**
- Status: ~ ACCEPTABLE WITH REVIEW
- Files: 4 (75% success)
- Records: 10 (50% overhead)
- Quality: MEDIUM
- Action: Manual OCR validation + org filter

## Files

Each batch has:
- `batch_N_results.json` — Raw extraction data
- `batch_N_report.md` — Detailed analysis & recommendations
- `batch_N_run.log` — Execution log

See `OVERNIGHT_SUMMARY.md` for comprehensive analysis.

## Key Findings

### False Positives by Batch
- B4: 10% (mostly mailing addresses) — ACCEPTABLE
- B5: 88% (org names, signatures) — NEEDS FILTERING
- B6: 97% (forms, addresses, blanks) — UNACCEPTABLE
- B7: 50% (OCR artifacts, org phones) — NEEDS REVIEW

### Extraction Quality Ranking
1. Batch 4 — High confidence, production-ready healthcare/HR data
2. Batch 7 — Medium confidence, sparse but valid individual records
3. Batch 5 — Low confidence, heavy org overhead but identifiable subjects
4. Batch 6 — Very low confidence, mostly organizational data

### Technology Notes
- Regex extraction: 0.01-0.42s per structured PDF
- OCR fallback: 1-10s per scanned PDF
- Email extraction: <0.01s per MSG file
- Spreadsheet extraction: <0.01s per worksheet

## Recommendations

### Immediate Action (Within 1 hour)
- Deploy Batch 4 to notification workflow (1,083 records)

### Short-term (Within 24 hours)
- Manually review Batch 7 records (10 records, 4 subjects validated)
- Re-run Batch 5 with org deny-list applied
- Determine scope for Batch 6 (clarify if org addresses are in breach scope)

### Medium-term (Before next batch cycle)
1. Implement org address/phone deny-list
2. Build form template filter (exclude blanks, boilerplate)
3. Add OCR error detection (flag low-confidence person names)
4. Install missing codec: `pip install pillow-heif`

### Long-term
- Consider vision-based extraction for Batch 6 quality improvement
- Evaluate email vs. org metadata classification
- Build subject-linkage UI for manual review

## Next Steps

1. Review batch_4_results.json (production data)
2. Read OVERNIGHT_SUMMARY.md (full analysis)
3. Manual validation of Batch 7 (4 subjects)
4. Org filtering for Batch 5 (6 subjects after cleanup)
5. Decision on Batch 6 scope

See detailed reports in this directory.
