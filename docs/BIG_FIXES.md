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

---

## Group E — Gap-fill improvements (2026-04-22)

Tonight's verify run surfaced that gap_filler's fill rate is ~32% (11 filled / 23 unfilled on CMG's post-extraction gaps). Each unfilled gap is a page where we lost a likely member. These five changes should push fill rate to ≥70%.

**No-regression principles that apply to every Group E change:**

1. **Additive, not replacing.** Each new path runs as a fallback when the current path returns empty. Never replace an already-working extraction.
2. **Gated on segregation signals.** Vision fallback only fires when segregation says the page *should* have PII — otherwise we'd waste vision calls on truly empty pages. Prompt changes preserve old prompt as a fallback when contract is missing.
3. **Budgeted.** Every new LLM / vision call counts against the same `max_llm_total` budget the gap filler already respects. No sprint can make a 100-page doc run for 3 hours.
4. **Measurable.** Each fix logs a new line (e.g. `Gap fill: vision fallback filled N pages`) so we can A/B the impact on the next run.
5. **Reversible.** Each fix is behind a setting flag if feasible (e.g. `GAP_FILL_VISION_FALLBACK_ENABLED=true` default true) so we can disable instantly if something regresses.

### E1 — Field-contract-aware gap-fill prompt (task #51)
**Change:** pass segregation's `field_inventory` + the gap's `expected_field` into the prompt. Current prompt says "find primary subject" generically; new prompt says "This page is expected to contain [UK_NINO, DATE_OF_BIRTH, PERSON, LOCATION]. Find them."

**Regression guard:** if `field_inventory` is None (no segregation data), fall back to old generic prompt. Never replaces the prompt, only augments it.

**Expected impact:** biggest single lift — LLM stops returning empty arrays when it doesn't know what to look for.

### E2 — Per-page vision fallback on empty text-LLM results (task #52)
**Change:** when `_fill_batched_text()` returns empty for a page AND that page has <30 chars of text extractable AND gap has an `expected_field` → render page at 300 DPI, send to 90B vision model with geo-neutral prompt (reuse `completeness_checker._vision_recover` logic).

**Regression guard:** Max 15 vision calls per doc (same bucket as completeness). Only fires when text is truly empty (not when text is present but LLM returns empty — that's E1's territory). Page has to be on segregation's expected-PII list.

**Expected impact:** catches form-PDF gaps (values in AcroForm, not text stream) that currently stay unfilled forever.

### E3 — Self-correct loop: lower threshold + feed diagnosis forward (task #53)
**Change:** raise self-correct trigger from `fill_rate < 0.30` to `fill_rate < 0.50`. Also: the diagnosis LLM's output ("these pages missed because X") gets passed as a context hint into the retry prompt instead of discarded.

**Regression guard:** self-correct already has its own budget counter. Threshold bump just makes it fire more often on borderline runs — it can't run more than once per doc.

**Expected impact:** runs at ~32% fill rate (like tonight) now trigger a diagnose-and-retry pass. Diagnosis-as-hint gives the retry prompt specific targeting instead of rerunning the same broken prompt.

### E4 — Gap detector cross-checks against PDF-structure-expected count (task #54)
**Change:** gap_detector stops flagging pages as "empty_page" gaps if the PDF structure (markers, gov-ID density from A2's estimator) shows those pages are boilerplate/continuation. Reduces false-positive unfilled count.

**Regression guard:** only DOWN-grades gap severity (from "empty_page" to "structural_continuation"). Never suppresses a real gap on a page that contains a member marker.

**Expected impact:** the "23 unfilled" metric gets more honest. If 8 of them are boilerplate continuation pages, the real fill rate is 11/(11+15) = 42%, not 32%.

### E5 — Roster→pages map reused by gap_filler (task #55)
**Change:** completeness_checker's `_build_name_pages_map` (with variants fix from e71bc22) tells us which page contains which named person. Gap filler ignores this. Change: when a gap page contains a roster name not yet in output, inject "this page contains 'Ms S Bamert' — find her gov ID + DOB" into the prompt.

**Regression guard:** if no roster exists (small docs, no completeness run), skip enhancement — behave as before.

**Expected impact:** targeted re-extraction for specifically-missed people. The Bamert/Bloom class of bugs disappears because we're now asking the LLM by name.

### Execution order for Group E

**After** tonight's verify run lands (so we see the honest baseline + what's still broken):

1. **E1** first — biggest win, smallest code change. Re-run Batch D, measure.
2. **E2** next — catches the form-PDF class of misses E1 can't touch.
3. **E3 + E4** together — both are tuning, both small.
4. **E5** last — depends on completeness roster being built; adds targeting but won't help if E1+E2 already got the same pages.

After each fix, re-run Batch D and independently verify against PDF ground truth before moving to the next. **No batched shipping of E1-E5 as a bundle** — we caught a regression that way last time. One fix, one verification.

### Group E success criteria

- Gap fill rate ≥ 70% (tonight baseline: 32%)
- No regression on existing Batch D recall (must stay ≥95% after Group A/B/C shipped)
- No regression on Batch A/C completeness (small docs, not dependent on gap-fill)
- Vision calls per doc stay within `COMPLETENESS_VISION_MAX_PAGES` budget (no runaway)

---

## Group I — Extraction prompts adapt to doc contract (2026-04-23)

### I5 — Strategy A → B fallback on undercount (task #65, shipped)

**Why:** J_crystal_report_payroll — Strategy A with marker "Employee ID"
(a column header) extracted only 3 records from 30 employees. The
snippet filter narrowed around the HEADER, capturing only row 1 per
page. Strategy A returned "3 records" which counts as success → never
fell through to Strategy B.

**Fix:** after Strategy A output, compute expected count = rpp × pages.
If actual < 30% of expected AND rpp > 1 (multi-person doc expected),
force Strategy B to run by clearing the records list.

**Generalizes to:** any tabular doc whose marker is a column header
rather than a per-person label (payroll, badge logs, patient lists,
etc.). No doc-specific code.

### I6 — Gov ID classifier respects doc contract (task #66, shipped)

**Why:** Classifier labeled STU9634863 as US_SSN (9 digits matched
loose regex), MEM31135606 as US_SSN (same), 992042 as
US_DRIVER_LICENSE. Contract from segregation said STUDENT_ID /
INSURANCE_ID / PATIENT_ID — classifier ignored it.

**Fix:** `infer_gov_id_type(raw, country_hint, contract_field_types)`
now takes the contract list. When the contract includes an
institutional type (STUDENT_ID, EMPLOYEE_ID, INSURANCE_ID,
PATIENT_ID, POLICY_NUMBER, etc.), returns the protocol-aliased form
(STUDENT_ID → FERPA_STUDENT_ID, INSURANCE_ID → PHI_HEALTH_PLAN,
PATIENT_ID → PHI_MRN) — NOT US_SSN just because digits align.

Deduplicator passes the union of `entity_types_found` across all
records as the contract.

**Verified 8 scenarios:** STUDENT_ID, INSURANCE_ID, PATIENT_ID,
EMPLOYEE_ID all correctly classified even when values look SSN-shaped.

### I10 — Classifier infers from doc_type when contract is generic OTHER_ID (task #70, shipped)

**Why:** I8 retest showed L_badge_access_log.csv still labelled
US_SSN because segregation's field_inventory was `["OTHER_ID",
"PERSON"]` — the generic escape hatch — not `["EMPLOYEE_ID"]`. I8's
contract check fell through, classifier hit regex, EMP299803 matched
nothing specific → extractor's US_SSN default survived.

**Fix:** when contract_field_types contains generic tokens
(`OTHER_ID`, `GOV_ID`, `IDENTIFICATION`, `NATIONAL_ID`, `TAX_ID`,
`GOVERNMENT_ID`), use segregation's `document_type` as a tiebreaker:

```
doc_type contains …      → inferred label
──────────────────────── ───────────────────
student / transcript /   → FERPA_STUDENT_ID
  school / academic /
  report_card / grade

patient / medical /      → PHI_MRN
  ehr / clinical /
  discharge / lab_result

insurance / policy /     → PHI_HEALTH_PLAN
  coverage / benefit /
  claim / policyholder

employee / staff /       → EMPLOYEE_ID
  payroll / badge /
  access / hr_ /
  log_file / timesheet

court / case / docket /  → CASE_NUMBER
  legal / litigation
```

Substring match — handles "payroll_summary_report",
"ehr_patient_list", "badge_access_log" without per-doc branching.

**Verified 8 scenarios.** Specific contracts (STUDENT_ID,
EMPLOYEE_ID) still win — I10 only fires on generic OTHER_ID cases.

### I9 — _merge_into upgrades government_id_type to more-specific label (task #69, shipped)

**Why:** I8 fix was invisible on retest because `_find_existing`
matched old pre-I8 subjects (labelled US_SSN) and `_merge_into`
preserved the old label. Re-running the pipeline could never fix
a mislabeled subject.

**Fix:** `_merge_into` now ranks the incoming `government_id_type`
against the existing one and takes the more-specific label:

```
Tier 3 (take): FERPA_STUDENT_ID, PHI_MRN, PHI_NPI, PHI_HEALTH_PLAN,
               EMPLOYEE_ID, POLICY_NUMBER, etc.
               (institutional / protocol-aligned — best evidence of
               doc-context understanding)
Tier 2 (take if new is T3): US_SSN, UK_NINO, IN_AADHAAR, BR_CPF,
               any country-prefixed label
Tier 1 (take if new is T2+): GOVERNMENT_ID / unknown fallback
Tier 0: null / empty — overwritten by anything
```

**Implications:**
- Re-extraction of a doc can now FIX bad historical labels
- Doesn't downgrade: if existing is PHI_MRN and incoming says US_SSN
  (weaker), existing wins
- Decouples extraction quality from first-seen bias

### I8 — Classifier contract from segregation, not just extractor (task #68, shipped)

**Why:** I6 wired the classifier contract to `r.entity_types_found`
which the extractor sets (narrow — mostly PERSON + US_SSN). When
segregation correctly identified a doc as EMPLOYEE_ID/BADGE_ID but
the extractor's output didn't carry that forward, the classifier
fell back to regex-matching `EMP299803` → labelled US_SSN.

**Fix:** deduplicator now unions two sources into the contract:
- `records[*].entity_types_found` (as before — extractor's output)
- `documents.metadata_json.segregation.field_inventory` per source
  document (authoritative contract from segregation)

Per-doc lookup runs once per unique source_document_id seen in the
group (no N² work). Best-effort — tolerates missing metadata.

**Expected impact on badge log retest:** EMP299803 now labelled
EMPLOYEE_ID not US_SSN, because segregation flagged EMPLOYEE_ID in
field_inventory.

### I7 — Strict gov-ID digit-count backstop (task #67, shipped)

**Why:** A_bank_statement extracted "123-45-678901" (11 digits) —
LLM concatenated two fields. The classifier's anchored regex already
downgrades this to GOVERNMENT_ID, but as defence-in-depth we reject
values with absurd digit counts at extraction time.

**Fix:** `_is_placeholder_ssn` rejects values with >16 digits
(no personal gov ID is that long).

**Not a cure-all:** values with 10-12 digits that aren't real IDs
still pass through extraction; relies on the classifier's strict
pattern matching to downgrade them to GOVERNMENT_ID so they don't
falsely trigger US_SSN protocols.

### I4 — Schema-driven extraction prompts (task #64, shipped)

**Why:** I3 hardcoded US-centric guesses: "123-45-6789" example, "SSN
Last 4" mention in the SSN description, `records_per_page ≥ 10` floor
for tabular docs. Wrong for Indian/UK/German/Australian docs. User
challenge: "What if you get an Indian payroll receipt or an
Australian or UK or German?"

**Fix:** the DocumentSchema LLM already analyzes each doc and
captures `records_per_page_estimate` + `field_map[i].value_example`.
Both were being ignored by the extractor. Now:

- `extract_with_markers` + `extract_text_batch` accept a
  `schema_field_map` param.
- `_build_gov_id_prompt_fragment(field_inventory, schema_field_map)`
  takes the schema path first: finds the gov-ID FieldContext,
  uses ITS label + value_example directly as the prompt's description
  + example. No doc-type branching, no geography assumptions.
- Caller in two_phase.py passes `schema.records_per_page_estimate`
  when present — overrides the marker detector's guess.
- I3's tabular floor demoted to fallback: fires only when rpp=1 AND
  no DocumentSchema is available AND doc_type name hints at tabular.
  Lowered the bump to 5 (not 10) — the schema is the real source
  when it's there.

**Verified across 5 geographies/doc types:**

| Doc | schema example | Prompt says |
|---|---|---|
| US payroll last-4 | "3274" | gov_id looks like '3274' |
| Indian Aadhaar last-4 | "1234" | gov_id looks like '1234' |
| UK pension full NI | "YB146386C" | gov_id looks like 'YB146386C' |
| German Steuer-ID | "12345678901" | gov_id looks like '12345678901' |
| No schema (fallback) | heuristic | MRN/SSN/NI defaults |

Same code path, same pipeline, different example per doc. LLM learned
the format from the doc during understanding phase; extraction just
repeats it back.

### I3 — Tabular payroll extraction + Last-4 SSN (task #62, shipped)

**Why:** J_crystal_report_payroll.pdf has 30 employees in tabular
format across 3 pages. Strategy A extracted only 3 records (10%
recall) with 0 gov_ids. Two issues:

1. **records_per_page=1** from marker detector — Strategy A prompted
   for "one person per page" instead of "extract all rows". 27
   employees silently dropped.
2. **Last-4 SSN format** (e.g. "SSN (Last 4): 3274") wasn't captured —
   the prompt asked for "123-45-6789" format, so the LLM skipped the
   4-digit values.

**Fix:**

- two_phase.py deterministic tabular floor: if segregation's
  `document_type` matches any of `payroll`, `register`, `roster`,
  `list`, `patient_list`, `employee_list`, `member_list`, `roll`,
  `ledger`, `enrollment`, `directory`, `access_log`, `log_file` —
  force `records_per_page ≥ 10`. Strategy A prompt then asks for "~10
  persons per page, return one object PER PERSON, not per page".
- text_batch_extractor.py: SSN description now explicitly accepts
  full / masked / last-4 formats, with example "'3274' in a payroll
  register". Same generic `gov_id` JSON key.

**Why this generalizes:** no hardcoded doc paths. Any doc type whose
`document_type` matches a tabular signal gets the multi-row prompt.
Last-4 support applies universally, since state laws (CA SB 24, NY
Shield) treat name+last-4-SSN as notifiable.

**Expected impact on next crystal payroll run:** 30 records extracted
(up from 3), 30 last-4 SSNs captured (up from 0). Under
state_breach_generic all 30 trigger notification.

### I2 — PHI/FERPA label normalization for protocol triggering (task #61, shipped)

**Why:** HIPAA protocol triggers on `PHI_MRN` but segregation emits
`MEDICAL_RECORD`. Same mismatch for FERPA (`FERPA_STUDENT_ID` vs
`STUDENT_ID`), HITECH, CCPA. Result: 20 EHR patients extracted with
MRNs would still never trigger HIPAA breach notification.

**Fix:** `PROTOCOL_TRIGGER_ALIASES` map in `gov_id_classifier.py`:

```
MEDICAL_RECORD  → PHI_MRN
MRN             → PHI_MRN
PATIENT_ID      → PHI_MRN
NPI_NUMBER      → PHI_NPI
MEDICAL_LICENSE → PHI_NPI
MEDICARE_NUMBER → US_MEDICARE_MBI
DEA_NUMBER      → PHI_DEA
INSURANCE_ID    → PHI_HEALTH_PLAN
ICD10           → PHI_ICD10
STUDENT_ID      → FERPA_STUDENT_ID
ENROLLMENT_ID   → FERPA_STUDENT_ID
```

New helper `normalize_protocol_label(label)` returns the canonical
form. Deduplicator applies it when building `pii_types_found` — keeps
both original and normalised labels so doc-type observability is
preserved AND protocols trigger.

**Verified:** 9-scenario test passed. EHR + medical → HIPAA triggers.
Report card → FERPA triggers. HR (EMPLOYEE_ID, no protocol) → no
trigger (correct). Bank (SSN) → HIPAA + state_breach both trigger.



### I1 — Generic gov_id output key, contract-aware descriptions (task #60, shipped next)

**Why:** Audit after taxonomy sweep found 27 gov IDs in source PDFs
that extraction missed — 20 MRNs in J_ehr_patient_list.pdf, 4 MRNs in
E_discharge_summary.pdf, 1 SSN in K_collection_notice.pdf, 2 SSNs in
T_inconsistent_redaction.pdf (got 1 of 2), 1 SSN in T_foia_style.

Root cause: Strategy A/B/gap-fill prompts used `"ssn"` as the output
JSON key with `"123-45-6789"` as the example. When segregation flagged
MEDICAL_RECORD or STUDENT_ID or UK_NINO, the LLM saw:
  - prompt: "extract the ssn field with SSN format"
  - input: a medical record with `"MRN: 1234567"`
  - LLM: confused, returned nothing or wrong value

**Fix:** single output key `"gov_id"` across all prompts, with
contract-aware description + example:

  Contract says MEDICAL_RECORD → prompt says "Medical Record Number
    (MRN), Patient ID, Medicare/Medicaid number, or insurance ID",
    example "MRN12345678"
  Contract says STUDENT_ID → prompt says "Student ID or Enrollment
    ID", example "STU0012345"
  Contract says UK_NINO → prompt says "UK National Insurance Number
    or NHS Number", example "YB123456C"
  Contract says IN_PAN/AADHAAR → prompt says "Indian PAN / Aadhaar /
    GSTIN", example "ABCPD1234E"
  Contract says US_DRIVER_LICENSE/PASSPORT → example "A12345678"
  Contract says POLICY_NUMBER → prompt says "Insurance policy number,
    claim number, or policyholder ID"
  Contract says EMPLOYEE_ID/BADGE_ID → "Employee/Badge ID",
    example "EMP1234567"
  Contract says US_SSN or generic GOV_ID/TAX_ID → example
    "123-45-6789" (SSN format, default)

Implementation: new helper `_build_gov_id_prompt_fragment(field_inventory)`
in `app/pipeline/text_batch_extractor.py` — dispatches to the right
example based on segregation contract. Strategy A calls it. Strategy B
prompt updated to use `"gov_id"` key. Gap filler's self-correct prompt
updated similarly. Parser accepts both `gov_id` (new) and `ssn`
(legacy) keys for backward compatibility.

**Expected impact:** next taxonomy run should recover the 24 missing
MRNs + 3 SSNs = 27 gov IDs that were in the PDFs but the LLM failed
to extract.

**Validation:** 10-contract helper test passes — SSN / NI / MRN /
student_id / badge / PAN / DL / policy_number / empty / no-gov-id
all map to appropriate example values. Same pipeline, adaptive
prompts.

---

## Group J — Protocol-driven pipeline (2026-04-24)

### J1 — Dedup min-PII threshold honors active protocol + customer config (task #63 v1, shipped)

**Why:** Customer custom protocols exist (via `protocol_configs` DB
table — each row has `target_entity_types`, `dedup_anchors`,
`confidence_threshold`, `export_fields`) but the pipeline ignored
them almost everywhere. Example: an Indian hospital configures a
DPDPA investigation targeting `[IN_AADHAAR, PHI_MRN,
PRESCRIPTION_ID]` — pipeline extracts values, but dedup's min-PII
threshold has no idea those types count as corroborating PII, so
subjects get filtered out.

**Fix v1:** `Deduplicator.__init__` accepts `protocol` + `protocol_config`.
At build time the corroborating set becomes:

```
effective_corroborating =
      _CORROBORATING_PII_TYPES      (127 gov + industry, H1+I2)
    | contract_types                 (per-group segregation contract, I8)
    | protocol_corroborating         (J1: protocol.triggering +
                                          protocol_config.target)
```

So a customer adding a new gov ID type to their protocol config
immediately flows into the dedup filter — zero code change required
per protocol.

**Backward compat:** `None` protocol / empty config leaves behaviour
unchanged. Verified against 3 scenarios: custom protocol (IN_AADHAAR
+ LOYALTY_CARD_NUMBER) adds 5 types, no-protocol adds 0, protocol-
only adds triggering types.

**What's NOT in J1 v1** (future work on same task):
- Extraction prompts don't yet use protocol targets (they use
  segregation contract + schema, which is usually enough)
- Dedup match keys don't yet use `protocol_config.dedup_anchors`
- Export schema doesn't yet use `protocol_config.export_fields`
- `confidence_threshold` from protocol_config not yet honored

These are incremental additions — ship and verify v1 first.

---

## Group H — Filter alignment across geographies + industries (2026-04-22)

### H1 — Minimum-PII threshold sync'd with gov_id_classifier (task #59, shipped `8a1830a`)

**Why:** Taxonomy sweep surfaced 18 of 42 PII docs producing 0 subjects.
Root cause: `_CORROBORATING_PII_TYPES` in deduplicator was a static
frozenset (~30 types) missing:
- Gov-ID types the `gov_id_classifier` already knew (50+ countries)
- Legacy aliases (`NI_NUMBER` for UK_NINO, `AADHAAR` for IN_AADHAAR,
  `PAN_CARD` for IN_PAN, etc.)
- Industry-specific identifiers (MEDICAL_RECORD, STUDENT_ID,
  BADGE_ID, POLICY_NUMBER, CASE_NUMBER, etc.)

Concrete misses: EHR patient list (20 patients), badge access log
(83 entries), report cards (4 students), payroll register (30
employees) — all extracted correctly, all dropped by the filter.

**Architecture (single source of truth for gov IDs):**

```
gov_id_classifier.py  ──  owns gov-ID knowledge
  _PATTERNS                    55 canonical types
  ALIAS_TO_CANONICAL           17 legacy aliases
  SUPPORTED_TYPES              canonical only
  EXPANDED_KNOWN_TYPES         canonical + aliases (72)
  is_known_gov_id_label()      shared helper
          │
          │ imports EXPANDED_KNOWN_TYPES
          ▼
deduplicator.py       ──  owns industry ID knowledge
  _NON_GOV_CORROBORATING       55 non-gov identifiers
    (medical MRN/NPI, education student_id, HR badge_id/employee_id,
     insurance policy_number, legal case_number, finance account_number,
     biometric, telecom device_id, SaaS customer_id, etc.)
  _CORROBORATING_PII_TYPES = gov | industry = 127 total
  + per-group contract override from PIIRecord.field_contract
```

**Sync rules going forward:**
- New country's gov ID → add pattern to `_PATTERNS` → flows to dedup filter automatically
- New legacy alias → add to `ALIAS_TO_CANONICAL` → flows automatically
- New industry-specific ID (not gov-issued) → add to `_NON_GOV_CORROBORATING` in deduplicator
- Truly novel doc-specific ID → segregation detects it → per-group contract override passes it (no code change)

**Classifier additions:**
- `US_DRIVER_LICENSE` pattern (permissive — 50 state formats)
- `US_PASSPORT` pattern

**Verification:** 15-label sync check passed across US / UK / India / Brazil / Italy / Netherlands / Canada + healthcare / education / HR / insurance.

**Expected impact:** the 18 zero-subject docs should produce real subjects on re-run. EHR → 20, badge log → 50+, payroll → 30, report card → 4, etc.

---

## Group G — Observability (2026-04-22)

### G1 — Per-job diagnostic script (task #58)

**Why:** production issue triage needs fast, deterministic answers. The
pipeline generates rich logs + metadata but no tool consolidates them.
Pure-LLM diagnostics hallucinate on numeric facts (how many subjects
are missing, which pages failed). Code-first stays honest.

**Location:** `scripts/diagnose_job.py <job_id>`

**Deterministic checks (code-only, no LLM for factual claims):**

1. **Per-doc structural ground truth vs extracted.** Open the PDF, regex-
   scan for unique gov-ID patterns (US_SSN, UK_NINO, Aadhaar, PAN, etc.),
   compare against unique canonical_government_id values in
   notification_subjects for that doc. Report recall %.

2. **Page coverage map.** Which pages produced records, which didn't.
   Cross-reference with segregation markers — flag pages that had
   markers but no record output as silent misses.

3. **Segregation sanity check.** For every doc with `pii_detected=False`,
   regex-scan the PDF text for obvious PII patterns (SSN, NI, DOB,
   phone, email). Flag probable false negatives (e.g.
   `I_ssn_card_copy.pdf` tonight).

4. **Validator reinstatement stats.** Parse logs for
   `N LLM-purges reinstated by anchor-count safety net` per doc.
   Surface docs with high reinstatement counts (proxy for LLM
   non-determinism intensity).

5. **Gap-fill breakdown.** From logs: text fills vs vision fills,
   `[E2]` stubborn-page recoveries, `[E5]` roster-name injections,
   unfilled pages. Flag docs with no gap-fill attempt when gaps exist.

6. **Dedup collapse ratio.** For each doc, `raw_records → subjects`
   ratio. High ratios (>3:1) might mean over-aggressive merging;
   ratios near 1:1 mean dedup barely fired.

7. **Contract vs extracted fill rates.** For each expected field type
   in segregation's field_inventory, what % of subjects for that doc
   have it populated. Highlights doc types where a field consistently
   fails (e.g. DOB empty 80% of the time on W2s).

8. **E4 delta summary.** From logs: `[E4] delta=±N` per doc. Aggregate
   across the run — total expected vs total extracted.

**Optional LLM narrative section (clearly separated from facts):**
After the deterministic report, prompt an LLM with the numeric
findings and ask for likely root-cause hypotheses. LLM outputs are
flagged `[HYPOTHESIS]` so auditors know they're not facts.

**Output formats:**
- Markdown report (auditor-readable)
- JSON (machine-readable, for piping into dashboards or tests)

**Usage:**
```
python scripts/diagnose_job.py <job_id>              # full report
python scripts/diagnose_job.py <job_id> --doc NAME   # single-doc
python scripts/diagnose_job.py <job_id> --json       # JSON output
python scripts/diagnose_job.py <job_id> --no-llm     # facts only
```

**Why code-first matters:**
LLMs hallucinate numeric claims ("this doc has 34 NI numbers"). A
regex scan doesn't. For prod triage the first answer must be
factually correct even if blunt. LLM narrative comes at the end as
a human-readable summary, never as the ground truth.
