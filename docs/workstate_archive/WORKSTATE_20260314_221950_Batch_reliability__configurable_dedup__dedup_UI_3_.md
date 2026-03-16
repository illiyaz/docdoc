# WORKSTATE.md — Live Task Checkpoint

## Current Task
Batch reliability + configurable dedup + dedup UI (3 problems)

## Task Status
- [x] Investigation complete
- [x] Fix 1: Batch reliability in llm_template_extractor.py
- [x] Fix 2: Auditor-configurable dedup
- [x] Fix 3: Frontend dedup strategy in review panel
- [x] Tests (32 new, 2177 total)
- [x] CLAUDE.md updated
- [x] COMPLETE

## Checkpoint Log
[2026-03-14 20:00:00] Investigation complete — 11/30 batches failed, 37% failure rate
[2026-03-14 20:15:00] Fix 1 implemented — retry+backoff, split-to-individual, _parse_json improved, model unloading
[2026-03-14 20:25:00] Fix 2 implemented — _build_anchor_key, active_anchors wired through pipeline
[2026-03-14 20:30:00] Fix 3 implemented — API returns dedup_anchors, frontend shows checkboxes
[2026-03-14 20:35:00] Tests pass: 32 new, 2177 total (0 failures)
[2026-03-14 20:40:00] CLAUDE.md updated, task complete

## Findings
### DB Investigation Results
- Job 2 (doc 869d7360): 30 batch calls, 19 valid, 11 FAILED (37% failure)
- 94/149 people extracted (63%). ~55 lost to JSON parse failures
- Failed batches: "Extra data" errors (multiple JSON values concatenated) + truncated responses
- Markdown fences (`json ... `) around responses not being stripped properly when combined with Extra data
- Latest job (118c45dc): 149 instances detected in preview, 0 extraction calls (extraction phase not triggered yet)

## Files Modified
- `app/structure/llm_template_extractor.py` ✓ — retry+backoff, _parse_json robustness, _build_anchor_key, _unload_unused_models, _try_close_truncated
- `app/pipeline/two_phase.py` ✓ — dedup_anchors from protocol config, timeout_s=120, pass active_anchors
- `app/api/routes/analysis_review.py` ✓ — returns {documents, dedup_anchors, protocol_name}
- `frontend/src/api/client.ts` ✓ — AnalysisResults interface, backward-compat array handling
- `frontend/src/pages/ProjectDetail.tsx` ✓ — dedup strategy section with read-only checkboxes
- `tests/test_api.py` ✓ — updated 2 tests for new response format
- `CLAUDE.md` ✓ — updated progress

## Files Created
- `tests/test_batch_reliability.py` — 32 tests

## Tests Status
2177 passed, 0 failed

## Last Updated
2026-03-14 20:40:00
