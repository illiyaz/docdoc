# Phase 1: UI Audit Findings — Forentis AI Field Selection UX

**Date:** 2026-04-06
**Auditor:** Automated UI Audit (scheduled task)

## 1. Auditor Workflow Map

The auditor sees the following screens in order:

1. **Dashboard** (`Dashboard.tsx`) — overview of queue counts, recent jobs, system health
2. **Projects** (`Projects.tsx`) → **ProjectDetail** (`ProjectDetail.tsx`) — manages documents, protocols, runs analysis/extraction jobs
3. **QueueView** (`QueueView.tsx`) — review tasks organized by queue type (low_confidence, escalation, qc_sampling, rra_review). Assign & complete tasks inline.
4. **SubjectDetail** (`SubjectDetail.tsx`) — individual notification subject: identity, PII inventory, merge history, notification preview, audit trail
5. **Diagnostic** (`Diagnostic.tsx`) — standalone PII scanner for ad-hoc file analysis (not part of main pipeline flow)

## 2. Where Field Selection Happens

Field selection and PII display occur in two distinct locations:

### A. SubjectDetail — "Data Elements Found" card (lines 98-114)
- Renders `subject.pii_types_found` as a flat list of `PIIBadge` components
- **No grouping** by category — badges displayed in array order
- **No frequency information** — no indication of how often a value appears
- **No person attribution** — no link between fields and which person they belong to
- **No suppression controls** — auditor cannot suppress org metadata from this view
- Data source: `GET /jobs/{jobId}/results` → `MaskedSubject.pii_types_found: string[]`

### B. ProjectDetail — Analysis Review Panel (within the Jobs tab)
- Shows per-document analysis with `sample_extractions`, `entity_groups`, `document_schema`
- Has detection review decisions (`DetectionDecision` type) for field-level include/exclude
- **This is the closest to field selection** but operates at document level, not subject level

### C. Diagnostic page — PII hit list
- Shows individual detections with confidence, layer, masked value
- Per-page breakdown, but only for ad-hoc single-file scans

## 3. Data Flow Gaps

### What the frontend receives (MaskedSubject):
```typescript
{
  subject_id: string
  canonical_name: string
  canonical_email: string      // masked
  canonical_phone: string      // masked
  pii_types_found: string[]    // flat list: ["ssn", "email", "address"]
  notification_required: boolean
  review_status: string
  merge_confidence: number | null
}
```

### What's available in the backend but NOT exposed:
- **`entity_role`** on Extraction model — "primary_subject", "related_party", "institutional"
- **`evidence_page`** on Detection/Extraction — which page(s) each field appears on
- **`confidence_score`** on Detection — detection confidence per field
- **`PersonContext`** from DocumentSchema — people with roles and context
- **`source_records`** on NotificationSubject — JSON linking back to extraction records
- **`source_document_name`** — which document the field came from
- **`detection_method`** — how the field was detected (presidio_pattern, llm, etc.)

## 4. Known Problems Confirmed

### Problem 1: Too many false positive fields shown
- **Root cause:** `pii_types_found` is an aggregated list from RRA. It includes ALL PII types detected across all source documents without filtering org metadata.
- **SchemaFilter** (`schema_filter.py`) does suppress org addresses/company phones during detection, but any that slip through end up in `pii_types_found` permanently.
- **No post-RRA suppression** exists — once a PII type is in `pii_types_found`, it stays.

### Problem 2: Can't easily map fields to people
- **Root cause:** `MaskedSubject` only carries `pii_types_found: string[]` — a flat list of type names with no person or role context.
- The backend has `entity_role` on Extraction and `PersonContext` in DocumentSchema, but neither flows to the results API.

### Problem 3: No auto-suggest or grouping
- **Root cause:** `PIIBadge` component has category awareness (PHI, Financial, Gov ID, Contact) via its `categoryColor()` function, but SubjectDetail renders badges in a flat `flex-wrap` container with no section headers.
- The category logic exists but isn't used for grouping.

### Problem 4: Repeated values not suppressed
- **Root cause:** No frequency analysis exists. The same address appearing on every page of a 20-page document will be detected 20 times, and while dedup at the extraction level may consolidate values, the pii_type "address" appears once in `pii_types_found` without any frequency context to help auditors distinguish org metadata from subject PII.

## 5. Component Inventory

| Component | Purpose | Relevant? |
|---|---|---|
| `PIIBadge.tsx` | Color-coded PII type badge | Yes — has category mapping |
| `MaskedField.tsx` | Displays masked values | Yes — shows canonical fields |
| `SubjectRow.tsx` | Row in subject list | Yes — shows first 3 PII types |
| `StatusBadge.tsx` | Review status indicator | Supporting |
| `AuditTimeline.tsx` | Audit event history | Supporting |
| `MergeExplanation.tsx` | Merge confidence details | Supporting |
| `NotificationPreview.tsx` | Notification template preview | Supporting |
| `DocumentViewer.tsx` | PDF viewer with bbox overlays | Supporting |
| `QueueCard.tsx` | Queue summary card | Not directly relevant |

## 6. Recommendations for Phase 2

1. Extend `_masked_subject()` API response with field metadata (frequency, person context, org flag)
2. Add `SchemaFilter.frequency_analysis()` method to identify repeated values
3. Group PII badges by category in SubjectDetail with collapsible sections
4. Add bulk suppression actions at category and org-metadata level
5. Show frequency indicators on fields that appear across many pages
