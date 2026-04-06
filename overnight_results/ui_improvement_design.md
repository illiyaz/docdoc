# Phase 2: UI Improvement Design — Field Selection UX

**Date:** 2026-04-06

## Overview

Five improvements to the auditor field selection experience, targeting the SubjectDetail page and supporting backend APIs.

---

## 1. Field Grouping by Category

### Current state
PII types displayed as a flat `flex-wrap` list of badges in the "Data Elements Found" card.

### Design
Replace flat badge list with collapsible category sections. Uses existing `PIIBadge` category logic (PHI_TYPES, FINANCIAL_TYPES, GOV_ID_TYPES, CONTACT_TYPES) as section organizers.

```
┌─────────────────────────────────────────────────┐
│ Data Elements Found                              │
├─────────────────────────────────────────────────┤
│                                                  │
│ ▼ Government IDs (3)                            │
│   [SSN] [DRIVERS_LICENSE] [PASSPORT]            │
│                                                  │
│ ▼ Contact Info (2)                              │
│   [EMAIL] [PHONE]                               │
│                                                  │
│ ► Healthcare (0)       ← collapsed, empty        │
│                                                  │
│ ▼ Financial (1)                                 │
│   [CREDIT_CARD]                                  │
│                                                  │
│ ▼ Other (1)                                     │
│   [DATE_OF_BIRTH]                               │
│                                                  │
└─────────────────────────────────────────────────┘
```

### Implementation
- New component: `SmartFieldFilter.tsx` (replaces inline badge rendering in SubjectDetail)
- Categories derived from `PIIBadge.tsx` sets: PHI_TYPES, FINANCIAL_TYPES, GOV_ID_TYPES, CONTACT_TYPES
- Empty categories collapsed by default, non-empty expanded
- ChevronUp/ChevronDown toggle per section

---

## 2. Person Attribution

### Current state
No person context in the results API. `pii_types_found` is an unattributed flat list.

### Design
Show which person each field is associated with, using `entity_role` from the Extraction model and `PersonContext` from DocumentSchema. New API field `person_context` groups fields by person.

```
┌─────────────────────────────────────────────────┐
│ ▼ Government IDs (3)                            │
│                                                  │
│   👤 John Doe (primary subject)                 │
│      [SSN]  [DRIVERS_LICENSE]                   │
│                                                  │
│   👤 Jane Doe (related party)                   │
│      [PASSPORT]                                  │
│                                                  │
└─────────────────────────────────────────────────┘
```

### Implementation
- Backend: new `person_context` field in masked subject response
- Structure: `{ person_name: string, role: string, pii_types: string[] }[]`
- Derived from `source_records` JSON on NotificationSubject + Extraction.entity_role
- Frontend: nested rendering within category sections

---

## 3. Frequency Indicator

### Current state
No information about how often a value appears across pages. Same address on every page looks identical to a unique personal address.

### Design
Badge showing page frequency: "18/20 pages" with color coding. High frequency (>80% of pages) signals likely organizational metadata.

```
┌─────────────────────────────────────────────────┐
│   [ADDRESS]  📊 18/20 pages  ⚠️ Likely org      │
│   [PHONE]    📊 18/20 pages  ⚠️ Likely org      │
│   [SSN]      📊 1/20 pages                      │
│   [EMAIL]    📊 2/20 pages                      │
└─────────────────────────────────────────────────┘
```

### Implementation
- Backend: new `field_frequency` field in masked subject response
- Structure: `{ pii_type: string, page_count: number, total_pages: number, is_org_metadata: boolean }[]`
- Computed by new `SchemaFilter.compute_field_frequency()` method
- Threshold for `is_org_metadata`: value appears on >80% of pages AND is address/phone/email type
- Frontend: inline frequency badge after each PIIBadge, amber coloring for suspected org metadata

---

## 4. Bulk Actions

### Current state
No bulk actions available. Auditor must review each field individually through the detection review decisions interface.

### Design
Action bar at the top of the field filter with three bulk operations:

```
┌─────────────────────────────────────────────────┐
│ [🚫 Suppress Org Metadata]  [✅ Approve High]   │
│ [⚠️ Flag for Review]                            │
├─────────────────────────────────────────────────┤
│ ...field list...                                 │
└─────────────────────────────────────────────────┘
```

- **Suppress all org metadata**: Marks all fields with `is_org_metadata=true` as suppressed
- **Approve all high-confidence**: Approves fields with confidence >0.90
- **Flag for review**: Marks remaining ambiguous fields for human review

### Implementation
- Frontend-only state management (local state in SmartFieldFilter)
- Actions produce a list of `field_decisions: { pii_type: string, action: "suppress"|"approve"|"review" }[]`
- Existing `DetectionDecision` API pattern reused for submission
- Each action shows a confirmation count: "Suppress 3 fields?"

---

## 5. Smart Suggestions

### Current state
No contextual intelligence. Auditor must manually identify patterns.

### Design
Inline suggestion banners when patterns are detected:

```
┌─────────────────────────────────────────────────┐
│ 💡 "123 Main St, Suite 400" appears on 18/20    │
│    pages — likely organizational. [Suppress]     │
│    [Keep] [Dismiss]                              │
├─────────────────────────────────────────────────┤
│ 💡 2 fields have low confidence (<60%).          │
│    [Flag for Review] [Dismiss]                   │
└─────────────────────────────────────────────────┘
```

### Implementation
- Derived from `field_frequency` data + confidence scores
- Rules:
  1. Value on >80% of pages + contact type → "Likely organizational. Suppress?"
  2. Field confidence <0.60 → "Low confidence. Flag for review?"
  3. >5 PII types on single subject → "High PII density. Review carefully?"
- Frontend: dismissible alert banners above the field list
- No new API calls — suggestions computed from existing enriched response data

---

## API Changes Summary

### Modified endpoint: `GET /jobs/{job_id}/results`

New fields added to each `MaskedSubject` in the response (all optional/nullable for backward compatibility):

```json
{
  "subject_id": "uuid",
  "canonical_name": "John Doe",
  "canonical_email": "***@***.***",
  "canonical_phone": "***-***-1234",
  "pii_types_found": ["ssn", "email", "address"],
  "notification_required": true,
  "review_status": "AI_PENDING",
  "merge_confidence": 0.95,

  "field_frequency": [
    { "pii_type": "address", "page_count": 18, "total_pages": 20, "is_org_metadata": true },
    { "pii_type": "ssn", "page_count": 1, "total_pages": 20, "is_org_metadata": false }
  ],
  "person_context": [
    { "person_name": "John Doe", "role": "primary_subject", "pii_types": ["ssn", "email"] },
    { "person_name": "Acme Corp", "role": "institutional", "pii_types": ["address", "phone"] }
  ]
}
```

### Backend changes
1. `schema_filter.py`: Add `compute_field_frequency(detections, total_pages) -> list[FieldFrequency]`
2. `jobs.py`: Extend `_masked_subject()` to include `field_frequency` and `person_context`
3. No DB schema changes — all computed from existing data (source_records JSON, Extraction table)

---

## File Changes Summary

| File | Change |
|---|---|
| `frontend/src/components/SmartFieldFilter.tsx` | **NEW** — grouped fields, frequency, bulk actions, suggestions |
| `frontend/src/pages/SubjectDetail.tsx` | Replace flat badge list with SmartFieldFilter |
| `frontend/src/api/client.ts` | Add FieldFrequency, PersonFieldContext types to MaskedSubject |
| `app/api/routes/jobs.py` | Extend `_masked_subject()` with field_frequency, person_context |
| `app/pii/schema_filter.py` | Add `compute_field_frequency()` static method |
| `tests/test_schema_filter_frequency.py` | **NEW** — tests for frequency computation |
