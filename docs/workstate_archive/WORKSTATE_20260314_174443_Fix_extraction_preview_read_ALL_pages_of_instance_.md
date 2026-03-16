# WORKSTATE.md — Live Task Checkpoint

## Current Task
Fix extraction preview: read ALL pages of instance 0, per-field page numbers from LLM, marker-based instance count.

## Task Status
- [x] Complete

## Checkpoint Log
[2026-03-14 10:00:00] Initial analysis complete. Found extraction_preview at two_phase.py:460-603.
[2026-03-14 10:05:00] Added build_preview_extraction_prompt to extraction_prompts.py
[2026-03-14 10:06:00] Rewrote extraction preview in two_phase.py (direct LLM call, _parse_preview_response)
[2026-03-14 10:07:00] Updated tests (21 tests, all pass)
[2026-03-14 10:08:00] Full test suite: 2138 passed. Updated CLAUDE.md.

## Files Modified So Far
- [x] `app/llm/extraction_prompts.py` — added `build_preview_extraction_prompt()` ✓
- [x] `app/pipeline/two_phase.py` — added `_parse_preview_response()`, `_PREVIEW_FIELD_MAP`, rewrote Stage 4b ✓
- [x] `tests/test_extraction_preview.py` — rewritten with 21 tests ✓
- [x] `CLAUDE.md` — updated test count + bugfix note ✓

## Tests Status
2138 passed, 0 failed

## Last Updated
2026-03-14 10:08:00
