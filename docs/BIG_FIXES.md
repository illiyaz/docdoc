# BIG_FIXES — Quality Attack List

**Created:** 2026-04-22 (after Batch D independent verification surfaced real gaps under an A+ claim)
**Goal:** ≥95% weighted recall on Batch D (CMG UK pension + AWIR US pension) without hand-coding doc-type-specific rules

## Tonight's ground truth (honest accounting)

| Doc | PDF truth | Extracted | Recall | Precision | Issues |
|---|---|---|---|---|---|
| AWIR-482 (US) | 20 real members | 21 rows (20 unique + 1 dup) | **100%** | 95% | Dup, 1 false-positive address |
| CMG pension (UK) | 34 real members | 26 subjects | **76%** | 100% | 5 purged, 2 never extracted, 1 lost in dedup |
| **Weighted** | **54** | **46 unique** | **85%** | **98%** | — |

The "47 subjects = A+" claim from the autonomous run was wrong — the pipeline's internal completeness metric (79%) was self-referential (measured against its own already-purged roster, not against PDF truth).

## The 9-fix attack list

### Group A — Critical recall (7-9% recall each, architectural)

**A1 — Move record_validator to AFTER gap-fill + completeness recovery**
- *Current:* validator runs first, purges 5 real CMG members, nothing downstream un-purges
- *Fix:* run validator as a FINAL pass over the combined output of all discovery paths
- *Expected impact:* +5 CMG subjects (Alford, Anjorin, P S Andrews, Austen, Amstell)
- *Size:* Medium (reorder pipeline stages + make sure validator's prompt still works on merged set)
- *Task:* #41

**A2 — Independent PDF-structure baseline for completeness check**
- *Current:* completeness compares `actionable / llm_roster` (both built from extraction output)
- *Fix:* count pages that LOOK like member pages via PDF structure (SUMMARY markers, standalone gov-ID patterns, repeating templates). Trigger recovery when `extracted < 0.85 × structural_estimate`.
- *Expected impact:* triggers recovery on CMG (would've caught Bamert + Bloom), catches future blind spots
- *Size:* Medium
- *Task:* #42

**A3 — Gap filler should CREATE records, not just fill fields**
- *Current:* gap filler only augments existing records; a page with a clear name+gov_id pattern not matching any existing record is ignored
- *Fix:* if gap-fill batch returns a `{name, gov_id}` tuple that matches no existing subject, add as new record
- *Expected impact:* recovers 1-3 CMG members when Strategy A misses them (Bamert p55, Bloom p100 candidates)
- *Size:* Small-Medium
- *Task:* #43

### Group B — Precision / dedup (1-2% recall each, localized)

**B1 — Dedup must normalize SSN mask variants in match key**
- *Current:* `XXXXX2682` ≠ `XXX-XX-2682` in `_find_existing()` → same person appears twice
- *Fix:* extract last-4 digits as the canonical match key regardless of mask format
- *Expected impact:* removes J T Browne false duplicate
- *Size:* Small
- *Task:* #44

**B2 — Surname-collapse prevention in dedup**
- *Current:* V Bhudia had NI+DOB+addr but got merged into D or R Bhudia → lost
- *Fix:* require distinct `canonical_government_id` OR exact `canonical_name` match for merge; reject merges that would combine 3+ distinct gov_ids under one surname
- *Expected impact:* recovers V Bhudia (1 CMG subject); prevents family-member collapse in future runs
- *Size:* Small
- *Task:* #45

**B3 — Page-header/footer pattern detection in address filter**
- *Current:* "77 450-MENDONCA" (CGS distribution code) appears on every AWIR page → extracted as an address for Stacey L Albright
- *Fix:* before accepting an address value, check whether the same string appears on >30% of pages in the doc. If yes, it's boilerplate, not an address.
- *Expected impact:* removes false positive; prevents similar errors on other repeating-header formats
- *Size:* Small
- *Task:* #46

### Group C — Remaining coverage gaps

**C1 — Diagnose Strategy A silent misses on Bamert (p55) + Bloom (p100)**
- *Current:* both pages have standard-format markers, yet never made it into extracted_records
- *Hypothesis:* pages_per_batch boundary (p100 is last page of 100) or snippet returned empty despite marker match
- *Fix:* instrument + add a "pages with marker hit but no record output" log line; likely small batching/off-by-one fix once root cause isolated
- *Expected impact:* recovers 2 CMG subjects directly, plus any future similar misses
- *Size:* Small after diagnosis
- *Task:* #47

**C2 — DOB format nondeterminism**
- *Current:* 2 AWIR subjects (Dorothy J Brown, J T Browne's 2nd entry) have null DOB despite PDF having values; DD/MM vs MM/DD mixing in extracted_records
- *Fix:* harden DOB normalizer for mixed-locale docs — infer format from `country` hint on the record, fall back to "both interpretations valid → keep year only" for ambiguous 2-digit months
- *Expected impact:* +2 DOB coverage on AWIR
- *Size:* Small
- *Task:* #48

### Group D — Infrastructure (not recall-blocking but needed)

**D1 — NotificationSubject table gets cleared between runs**
- *Observed:* today's `notification_subjects` table was empty (0 rows) while `notification_lists` had 4 jobs × N IDs; `/compare` endpoint returns 0 subjects on older runs
- *Hypothesis:* purge / archive path cascades beyond intended scope, or subjects are committed+rollback somewhere
- *Fix:* audit all DELETE paths on `notification_subjects`; add a "last touched by" trail so we can see who cleared them
- *Size:* Medium (investigation + fix)
- *Task:* #49

**D2 — Frontend wiring for /compare, /failed-docs, /segregation/flat**
- *Backend:* shipped (9280414 + d53c433)
- *Client:* shipped (cc29e4d)
- *UI:* not yet built
- *Size:* Medium
- *Task:* #50

## Execution order

1. **A1 first** (biggest win, architectural): moves validator to end → +5 CMG immediately
2. **A2 + A3 together**: independent baseline + record-creating gap fill → triggers real recovery, catches Bamert/Bloom + V Bhudia
3. **B1 + B2 + B3**: quick wins, precision bumps
4. **C1 + C2**: localized fixes after A/B run validates
5. **D1**: investigate while D runs
6. **D2**: after quality stabilizes

## Success criteria

Re-run Batch D after Group A + B lands. Expected:
- AWIR: 20 subjects, 0 duplicates, 0 false-positive addresses → **100% recall, 100% precision**
- CMG: 31-34 of 34 members → **91-100% recall**
- Weighted: **≥95% recall**
- Independent verification (PDF ground truth) must agree with pipeline's reported numbers — no more self-referential completeness claims
