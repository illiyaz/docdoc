# Code Changes — Overnight Pipeline Phase 3

**Date:** 2026-04-06

## Summary

Added three new filtering mechanisms based on false positive patterns observed during overnight batch extraction of 34 real documents (2,424 records).

---

## 1. ValueFrequencyFilter (`app/pii/schema_filter.py`)

**New class** that detects and suppresses PII values appearing on >80% of pages within a document, identifying them as organizational metadata (letterhead addresses, footer phone numbers, company emails).

**Key features:**
- `from_extractions(extractions, total_pages)` — builds filter from extraction records
- `is_org_metadata(value, pii_type)` — checks if a specific value is flagged
- `flagged_values` property — returns all flagged values for audit display
- Safety: `HighFrequencyValue.to_dict()` masks raw values
- Minimum 3-page threshold to avoid false triggers on short documents
- Only applies to eligible types: PERSON, LOCATION, PHONE_NUMBER, EMAIL_ADDRESS, ORGANIZATION, URL, FAX_NUMBER (never SSN, DOB, etc.)

**Also added:** `is_blank_or_placeholder()` — catches blank, underscore, dash, and dot-only patterns that should never be treated as valid PII values. Addresses the `____` false positive (52 occurrences in batch results).

## 2. Email Sender Context Detection (`app/pii/context_deny_list.py`)

**New function** `is_email_sender_context()` that detects PII appearing near email sender labels ("From:", "Regards,", "Best regards,", etc.) and flags it as organizational metadata rather than breach-subject PII.

**Addresses:** Ann Maynard (5x), amaynard@jswallacegroup.com (5x), (585) 482-8000 (5x) all appearing as false positives in MSG email extraction.

## 3. Label-as-Person Deny List (`app/pii/context_deny_list.py`)

**New function** `is_label_as_person()` with `LABEL_DENY_LIST` — catches common labels and category names that get misclassified as PERSON due to title-case or ALL-CAPS formatting.

**Addresses:** "Drug Test" (3x), "Group Rochester" (5x), "Johnstone Supply" (5x), "City of Federal Way", "Other Transactions".

---

## Files Modified

| File | Change |
|------|--------|
| `app/pii/schema_filter.py` | Added `ValueFrequencyFilter`, `HighFrequencyValue`, `is_blank_or_placeholder()` |
| `app/pii/context_deny_list.py` | Added `is_email_sender_context()`, `is_label_as_person()`, `LABEL_DENY_LIST` |
| `tests/test_value_frequency_filter.py` | **NEW** — 21 tests covering all new functionality |

## Test Results

```
tests/test_value_frequency_filter.py ... 21 passed in 0.08s
```

All files compile cleanly: `python3 -m py_compile` passes for both modified files.

## Integration Notes

These new filters are designed as composable functions that can be called from:
- `SchemaFilter.filter_detections()` — add calls to `is_blank_or_placeholder()` and `ValueFrequencyFilter.is_org_metadata()`
- `is_likely_false_positive()` — add calls to `is_label_as_person()` and `is_email_sender_context()`
- `forentis_extract.py` — add post-extraction filtering step

Full pipeline integration should be done in a follow-up step after human review of the overnight results.
