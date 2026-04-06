# Extraction Fixes — Phase 1 Post-Mortem

**Date:** 2026-04-07
**Test run:** Phase 1 — 13 small PDFs (<200 pages), 72 minutes
**Result:** 268 subjects extracted out of ~4,244 expected (6.3% capture rate)

---

## Critical Fixes (must do before Phase 2)

### Fix 1: Tabular docs mis-routed to coordinate path (~2,400 missing records)

**Problem:** Docs with multiple records per page (TPHS2 payroll = 8 SSNs/page, AWIR shareholder list = 20 accounts/page) are routed to coordinate extraction, which extracts ONE record per page and silently drops the rest.

**Affected docs:**
- TPHS2_656_0000070715.pdf — 1,041 SSNs across 132 pages, got 44
- AWIR-DOC.00000038.15.pdf — 308 accounts across 15 pages, got 5
- AWIR-DOC.00000001.00000038.00000806.pdf — 1,052 accounts across 50 pages, got 32

**Root cause:** VisionRouter/schema classifies these as "fixed layout" because every page looks identical, but the coordinate extractor assumes 1 person = 1 page. The `is_tabular` flag isn't being set when the field map has a repeating anchor.

**Fix location:** `app/pipeline/two_phase.py` lines 2601-2617 (coordinate path eligibility) and VisionRouter routing logic. Need to detect records-per-page > 1 during analysis (count SSN/name patterns per sample page) and route to `llm_table` instead.

**Validation:** TPHS2_70715 should yield ~1,000 subjects. AWIR 15 should yield ~300.

---

### Fix 2: LLM budget cap too low for table extraction (~900 missing)

**Problem:** `_MAX_LLM_BATCHES = 100` in `two_phase.py:2889` caps LLM calls. For 3046246 (158 pages, ~6 athletes/page = 975 persons), the learn-then-extract hybrid only recovered 66 records.

**Fix:** Scale budget based on doc size. Suggestion: `min(page_count * 2, 500)` instead of flat 100. Or: make the learn-then-extract regex fallback smarter (it currently only learns from the first 100 batches then applies patterns — those patterns need to catch more).

**Validation:** 3046246 should yield ~900+ subjects.

---

### Fix 3: Onset/field-map excluding valid pages (~600 missing)

**Problem:** AWIR-DOC.00000038.15 has data on all 15 pages but only pages 11-15 were extracted (5 subjects). Vamp0000072380 has 17 pages but only 5 extracted (pages 6-15).

**Root cause:** Either onset detection is too aggressive (skipping pages with data), or field map anchors only match on later pages. Need to check what `find_data_onset` returns for these docs and whether the coordinate field map anchor exists on page 0.

**Fix location:** Check onset in `app/readers/onset.py` for these specific docs. If onset is wrong, fix the onset heuristic. If field map anchors are the issue, the field map needs to be more flexible.

---

## Data Quality Fixes

### Fix 4: Phone field pollution (38 subjects have dollar amounts)

**Problem:** TPHS2_70715 coordinate field map maps financial columns (CUR REG ERN = `526.56 .00 .00 2`) to the phone field. Also 6 subjects have SSNs (`051-62-6661`) in the phone column.

**Root cause:** Field map anchor for "phone" is matching a financial column. The payroll layout has columns: `SOC SEC # | EMPLOYEE NAME | STATUS | RATE CODE | FREQ | CUR REG HRS | CUR REG ERN | CUR HOL HRS | CUR HOL ERN...`

**Fix:** Add phone format validation in coordinate_extractor — if extracted "phone" doesn't match a phone pattern (10-11 digits, optional country code, hyphens/parens), reject it. See `app/pipeline/coordinate_extractor.py`.

---

### Fix 5: Name truncation and leading commas (13 subjects)

**Problem:** Names from TPHS2_70715 look like `,Alexander Dixo` — leading comma, last name truncated.

**Root cause:** The coordinate field map bbox for PERSON is too narrow, clipping the name. The payroll format is `LAST,FIRST MIDDLE` and the bbox is missing the last few characters. The leading comma suggests it's capturing from mid-string.

**Fix:** Widen the name bbox in the field map, or fix the anchor-relative offset calculation. Also add post-extraction cleanup: strip leading/trailing commas and reject names under 4 chars.

---

### Fix 6: Zero DOB across all 268 subjects

**Problem:** Not a single date of birth was extracted. 3046246 (Special Olympics) has DOBs on every page (`10/26/1995`), yet none captured.

**Root cause:** Either the DOB field isn't in the field map, or the LLM table extractor isn't mapping the "Age" / date column to DATE_OF_BIRTH. Check schema.fields for DOB presence.

---

### Fix 7: Empty names — 66 subjects (25%) have no name

**Problem:** AWIR-DOC.00000098 = 30 nameless subjects, TPHS2_71913 = 12, TPHS2_70715 = 10.

**Root cause:** Extracting government IDs (SSN, driver license) without linking to a person entity. The RRA merge is creating orphan subjects from standalone ID detections.

**Fix:** In RRA, require at least a name OR a (name + ID) pair. Standalone IDs without any name association should be flagged for review, not promoted to subjects.

---

## Performance Notes

- 72 min for 13 docs on this hardware is reasonable
- Vision path (4 docs) consumed ~55% of total time
- Production with GPU (A100/H100): expect 15-25 min for same set
- Coordinate path is already fast (seconds per doc) — the issue is completeness not speed
- Fixing tabular routing would extract 10-15x more records WITHOUT proportionally more time

---

## Priority Order

1. **Fix 1** (tabular routing) — biggest impact, ~2,400 records
2. **Fix 2** (LLM budget) — ~900 records
3. **Fix 3** (onset narrowing) — ~600 records
4. **Fix 4** (phone validation) — data quality
5. **Fix 5** (name cleanup) — data quality
6. **Fix 6** (DOB mapping) — completeness
7. **Fix 7** (orphan IDs) — data quality
