# WORKSTATE.md — Live Task Checkpoint

## Current Task
Fix FieldMapBuilder spatial relationship bugs: wrong "lines_below_N" instead of "same_line_right"

## Task Status
- [x] Complete

## Checkpoint Log
[2026-03-17 10:00:00] Read field_map_builder.py and test_field_map_builder.py, analyzed bugs
[2026-03-17 10:05:00] Implemented all fixes in field_map_builder.py
[2026-03-17 10:08:00] Added 12 new tests, all 52 tests pass, safety tests pass, CLAUDE.md updated

## Findings
### Code Locations Found
- `app/pipeline/field_map_builder.py` — main file fixed
- `tests/test_field_map_builder.py` — tests extended

### Key Decisions Made
- Use first (topmost) value word y for spatial comparison, not merged bbox center
- Add near_label proximity-based match selection for duplicate values
- Increase _LINE_TOLERANCE from 5 to 8
- Dedup field maps by field_type, drop empty anchors
- Add fallback line_height=15 for tiny labels (<5pt)

## Files Modified So Far
- [x] `app/pipeline/field_map_builder.py` ✓
- [x] `tests/test_field_map_builder.py` ✓
- [x] `CLAUDE.md` ✓

## Files Created
None

## Tests Status
- 52/52 tests pass in test_field_map_builder.py (+12 new)
- 33/33 safety/schema/repository tests pass
- py_compile OK

## Remaining Work
All done.

## Blockers
None

## Last Updated
2026-03-17 10:08:00
