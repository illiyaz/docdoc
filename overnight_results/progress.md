# Overnight Pipeline Progress

**Started:** 2026-04-06 ~02:30 UTC
**Completed:** 2026-04-06 ~02:50 UTC

## Phase 1: Batch Extraction — COMPLETE

| Batch | Label | Status | Records |
|-------|-------|--------|---------|
| 1 | Simple PDFs | complete | 332 |
| 2 | AWIR legal docs | complete | 291 |
| 3 | Corporate docs | complete | 505 |
| 4 | TPHS + Vamp PDFs | complete | 1,083 |
| 5 | MSG email files | complete | 49 |
| 6 | Mixed formats | complete | 154 (1 error) |
| 7 | Remaining Vamp PDFs | complete | 10 |

**Total: 34 files, 33 success, 2,424 records**

## Phase 2: Aggregate Analysis — COMPLETE
See `aggregate_analysis.md`

## Phase 3: Smart Field Filtering — COMPLETE
- Added ValueFrequencyFilter (schema_filter.py)
- Added email sender context detection (context_deny_list.py)
- Added label deny list (context_deny_list.py)
- 21 new tests — all passing
See `code_changes.md`

## Phase 4: Synthetic Test Designs — COMPLETE
10 designs targeting specific weaknesses. Not generated — awaiting review.
See `synthetic_test_designs.md`

## Phase 5: Final Report — COMPLETE
See `OVERNIGHT_SUMMARY.md`
