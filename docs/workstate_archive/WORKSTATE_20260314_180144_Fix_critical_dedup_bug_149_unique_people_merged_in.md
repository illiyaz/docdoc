# WORKSTATE.md — Live Task Checkpoint

## Current Task
Fix critical dedup bug: 149 unique people merged into 28 rows.

## Task Status
- [x] Complete

## Checkpoint Log
[2026-03-14 00:00:00] Identified bugs: _deduplicate_records keys on name only, build_confidence same-doc safety net ignores page_range
[2026-03-14 00:01:00] Fixed _deduplicate_records with instance_aware param, fixed build_confidence cross-instance check
[2026-03-14 00:02:00] Updated table extraction callers to use instance_aware=False
[2026-03-14 00:03:00] All 2145 tests passing, CLAUDE.md updated

## Findings
### Code Locations Found
- `app/structure/llm_template_extractor.py:557` — `_deduplicate_records()`
- `app/structure/vision_extractor.py:128,186,242` — callers of `_deduplicate_records`
- `app/rra/entity_resolver.py:151-179` — `build_confidence()` cross-instance + safety net

### Key Decisions Made
- Template dedup: key on (name, page_range) — different instances = different people
- Table dedup: key on name only — same person can span pages
- EntityResolver: return 0.0 for same-doc different page_range pairs

## Files Modified So Far
- [x] `app/structure/llm_template_extractor.py` — instance_aware param on _deduplicate_records ✓
- [x] `app/structure/vision_extractor.py` — table/page callers use instance_aware=False ✓
- [x] `app/rra/entity_resolver.py` — cross-instance merge prevention (0.0 confidence) ✓
- [x] `tests/test_llm_extraction.py` — updated TestBatchDedup for instance-aware behavior ✓
- [x] `tests/test_vision_extraction.py` — split dedup test into same/different instance ✓
- [x] `tests/test_template_extraction_fix.py` — added TestCrossInstanceMergePrevention (5 tests) ✓
- [x] `CLAUDE.md` — documented bugfix, updated test count to 2145 ✓

## Tests Status
2145 passed, 0 failed

## Remaining Work
None — task complete

## Blockers
None

## Last Updated
2026-03-14 00:03:00
