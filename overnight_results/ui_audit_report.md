# Forentis AI — Overnight UI Audit Report

**Date:** 2026-04-06
**Task:** Auditor field selection UX improvements

## Executive Summary

Completed a 4-phase audit of the Forentis AI auditor frontend, identifying 4 known UX problems and implementing 5 improvements across backend and frontend. All changes are backward-compatible with existing API contracts.

## Verification Results

| Check | Result |
|---|---|
| `py_compile schema_filter.py` | PASS |
| `py_compile jobs.py` | PASS |
| TypeScript `tsc -b --noEmit` | PASS (0 errors) |
| `pytest test_schema_filter_frequency.py` | **20/20 PASS** |
| Frontend `npm run build` | tsc OK; vite build fails due to pre-existing rollup arm64 binary issue (not related to changes) |
| `test_safety.py` / `test_schema.py` | Not runnable in sandbox (Python 3.10 lacks StrEnum from 3.11; pre-existing) |

## Files Modified

| File | Action | Lines Changed |
|---|---|---|
| `app/pii/schema_filter.py` | MODIFIED | +120 lines: `FieldFrequency`, `PersonFieldContext`, `compute_field_frequency()`, `build_person_context()` |
| `app/api/routes/jobs.py` | MODIFIED | +65 lines: imports, `_compute_field_enrichment()`, updated `_masked_subject()` with db param |
| `frontend/src/api/client.ts` | MODIFIED | +14 lines: `FieldFrequency`, `PersonFieldContext` interfaces, added to `MaskedSubject` |
| `frontend/src/components/SmartFieldFilter.tsx` | **NEW** | 280 lines: grouped fields, frequency badges, bulk actions, smart suggestions |
| `frontend/src/pages/SubjectDetail.tsx` | MODIFIED | -10/+5 lines: replaced flat badge list with SmartFieldFilter component |
| `frontend/src/pages/ProjectDetail.tsx` | MODIFIED | 1 line: fixed pre-existing TS error (ReactNode type) |
| `tests/test_schema_filter_frequency.py` | **NEW** | 190 lines: 20 tests covering frequency, person context, safety |

## Improvements Implemented

### 1. Field Grouping by Category
PII types are now grouped into collapsible sections: Government IDs, Healthcare/PHI, Financial, Contact Info, Other. Each section shows count and is togglable.

### 2. Person Attribution
New `person_context` field in the API response groups PII types by person using `entity_role` from the Extraction model. Displayed inline within category sections.

### 3. Frequency Indicators
Each PII type badge shows how many pages it appears on (e.g., "18/20 pg"). High-frequency values (>80% of pages) are flagged with an amber warning.

### 4. Bulk Actions
Two bulk action buttons: "Suppress Org Metadata" (suppresses fields flagged as organizational) and "Restore All" (reverses suppressions). Client-side state management.

### 5. Smart Suggestions
Contextual suggestion banners appear when patterns are detected: high-frequency org metadata, high PII density. Each suggestion has actionable buttons (Suppress/Dismiss).

## Architecture Decisions

- **No DB schema changes**: All enrichment data computed from existing `Extraction` table and `source_records` JSON. No new migrations needed.
- **Backward compatible**: New `field_frequency` and `person_context` fields are optional in the API response. Existing clients see them as `undefined` and continue working.
- **Fail-safe enrichment**: `_compute_field_enrichment()` is wrapped in try/except — if enrichment fails, the base response is still returned.
- **No new dependencies**: Uses only existing libraries (dataclasses, SQLAlchemy queries).
- **Client-side suppression**: Bulk actions operate on local state only — no new API calls for field decisions (reuses existing `DetectionDecision` pattern when submitted).

## Known Limitations

- **Person context accuracy**: `entity_role` on Extraction is populated by the pipeline; documents processed before this feature won't have role data. Fallback displays the role name as a label.
- **Frequency analysis**: Requires `evidence_page` to be set on Extraction records. Documents with no page info show `1/1` frequency.
- **No persistence for suppressions**: Bulk suppress/restore actions are local to the component session. To persist, auditors use the existing detection review decision workflow.

## Deliverables

- `overnight_results/ui_audit_findings.md` — Phase 1 analysis
- `overnight_results/ui_improvement_design.md` — Phase 2 design document
- `overnight_results/ui_audit_report.md` — This report
