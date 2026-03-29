# Implementation Plan — Forentis AI (Active Steps)

For completed steps, see [PLAN_COMPLETED.md](PLAN_COMPLETED.md).
For project overview and conventions, see [../CLAUDE.md](../CLAUDE.md).
For detailed per-step implementation notes, see [CLAUDE_HISTORY.md](CLAUDE_HISTORY.md).

---

## Progress Summary

| Phase | Status | Tests |
|---|---|---|
| Phase 1-4 (Core + RRA + Protocols + HITL) | COMPLETE | ~1500 |
| Phase 5 Steps 1-24e (Extraction Engine) | COMPLETE | ~2800 |
| Phase 5 Steps 26-26d (LiteParse + Auditor Workflow) | COMPLETE | ~2850 |
| Phase 5 Step 29a (Notification Preview) | COMPLETE | ~2850 |
| **Phase 6 (Security + Governance)** | **NEXT** | — |
| Phase 7 (Workflow Completeness) | Pending | — |
| Phase 8 (Scale + Polish) | Pending | — |

**What's built:** 19 tables, 13 migrations, 78K PII records from 34 real docs.

**What's already landed that future steps build on:**
- Source document viewer with bbox overlays (Step 26b)
- Merge explanation with per-anchor signals (Step 26c)
- Notification email/letter preview with masked PII (Step 29a)
- Delivery status dashboard endpoint (Step 26d)
- Analysis review filter tabs, dedup summary, extraction progress bar (Step 26d)

---

## Phase 6 — Security + Governance

**Goal:** Make the tool deployable. No law firm adopts a tool that handles PII without login.

---

### Step 25 — Authentication, RBAC & Access Audit Logging

**Goal:** Every API call requires a verified identity. Every action is logged. The four roles are enforced.

#### 25a. Auth Backend — JWT + Local User Store

| File | What to do |
|---|---|
| `app/db/models.py` | Add `User` model: id, email, hashed_password, display_name, role, is_active, created_at, last_login_at |
| `alembic/versions/0014_users.py` | Migration for `users` table |
| `app/core/auth.py` | NEW — `hash_password()`, `verify_password()` (bcrypt), `create_access_token()`, `decode_token()` (PyJWT). Token expiry via settings. |
| `app/core/settings.py` | Add `jwt_secret_key`, `jwt_expiry_minutes=480`, `auth_enabled=true` |
| `app/api/routes/auth.py` | NEW — `POST /auth/login`, `POST /auth/register` (admin-only), `GET /auth/me`, `POST /auth/change-password` |
| `app/api/deps.py` | Add `get_current_user()` dependency — extracts JWT from `Authorization: Bearer`, returns `User` or 401 |
| `tests/test_auth.py` | Login, token expiry, bad credentials, password hashing |

**Air-gap safe:** No OAuth, no external IdP. Local user store. JWT secret from settings.

#### 25b. RBAC Enforcement on All Routes

| Role | Can do |
|---|---|
| `QC_SAMPLER` | Read-only: review queue, sampling results |
| `REVIEWER` | Above + approve/reject documents, review subjects |
| `LEGAL_REVIEWER` | Above + escalation decisions, regulatory protocol changes |
| `APPROVER` | Above + extraction, export, notifications, user management |

| File | What to do |
|---|---|
| `app/core/auth.py` | Add `require_role(*roles)` dependency factory — 403 if insufficient |
| All route files | Add `Depends(require_role(...))` to each endpoint |
| `tests/test_rbac.py` | Each role against each endpoint — verify 403 for insufficient privileges |

**Note:** `app/api/routes/documents.py`, `notifications.py`, `review.py` already have `# Phase 6: add Depends(get_current_user)` comments marking where auth goes.

#### 25c. Access Audit Logging

| File | What to do |
|---|---|
| `app/db/models.py` | Add `AccessLog` model: id, user_id, action, resource_type, resource_id, ip_address, timestamp |
| `alembic/versions/0015_access_logs.py` | Migration (append-only — no DELETE/UPDATE) |
| `app/api/middleware/access_log.py` | NEW — logs every authenticated request |
| `app/api/routes/audit.py` | Add `GET /audit/access-log` — paginated, APPROVER only |
| `tests/test_access_log.py` | Verify entries for all sensitive actions |

---

### Step 27 — Regulatory Deadline Dashboard

**Goal:** Countdown timers per active matter. The #1 number in breach response.

**Prerequisites:** Step 26d delivery dashboard already exists (endpoint + summary).

#### 27a. Breach Date Tracking

| File | What to do |
|---|---|
| `app/db/models.py` | Add to `Project`: `breach_discovered_at`, `breach_occurred_at` (DateTime, nullable) |
| `alembic/versions/` | Migration adding date columns |
| `app/api/routes/projects.py` | Accept breach dates in project create/update |
| `app/protocols/protocol.py` | Add `compute_deadline(discovery_date)` and `days_remaining(discovery_date)` |
| `tests/test_deadlines.py` | Test per-protocol deadlines (HIPAA 60d, GDPR 72h, state laws vary) |

#### 27b. Deadline Dashboard API & Frontend

| File | What to do |
|---|---|
| `app/api/routes/dashboard.py` | Add `GET /dashboard/deadlines` — active projects with days_remaining, status (on_track/at_risk/overdue) |
| `frontend/src/components/DeadlineCountdown.tsx` | NEW — "23 days remaining" or "OVERDUE by 4 days" |
| `frontend/src/pages/Dashboard.tsx` | Deadline widget: matters sorted by urgency, colour coded (green >14d, amber 3-14d, red <3d, black overdue) |
| `frontend/src/pages/ProjectDetail.tsx` | Deadline countdown at top of project view |
| `tests/test_dashboard_deadlines.py` | Deadline API, sorting, status computation |

---

## Phase 7 — Workflow Completeness

**Goal:** Complete the auditor workflow loop. Evidence packaging, batch send, re-extraction, manual merge/split.

---

### Step 28 — Evidence Package Export

**Goal:** One-click export: methodology PDF + notification XLSX + audit CSV + QC report → ZIP.

#### 28a. Methodology Report

| File | What to do |
|---|---|
| `app/export/methodology_report.py` | NEW — auto-generated PDF: engagement summary, document inventory, extraction methodology (which paths used per doc), verification results, dedup summary, QC results, notification summary |
| `app/export/evidence_bundle.py` | NEW — orchestrates: methodology PDF + notification XLSX + audit CSV → ZIP |
| `app/api/routes/exports.py` | Add `POST /exports/{job_id}/evidence-bundle` |
| `frontend/src/pages/ProjectDetail.tsx` | "Export Evidence Bundle" button |
| `tests/test_evidence_bundle.py` | Bundle generation, verify all files present |

#### 28b. XLSX Multi-Sheet Export

| File | What to do |
|---|---|
| `app/export/xlsx_exporter.py` | NEW — Sheet 1 "Notification List", Sheet 2 "Extraction Detail", Sheet 3 "Document Inventory", Sheet 4 "Audit Trail". openpyxl. |
| `app/api/routes/exports.py` | Add `format=xlsx` parameter |
| `tests/test_xlsx_export.py` | Multi-sheet generation, data integrity |

---

### Step 29b — Batch Approval & Send

**Goal:** APPROVER sign-off before batch notification delivery.

**Prerequisites:** Step 29a (notification preview) already complete.

| File | What to do |
|---|---|
| `app/api/routes/notifications.py` | `POST /notifications/lists/{list_id}/approve` (APPROVER role), `POST /notifications/lists/{list_id}/send` (trigger delivery) |
| `frontend/src/pages/ProjectDetail.tsx` | Notification tab: preview samples, approve batch, trigger send, show progress (sent/failed/total) |
| `app/audit/audit_log.py` | Log approval + send events |
| `tests/test_notification_batch.py` | Approval flow, send triggering, audit logging |

**Note:** `NotificationList` model already has `approved_at` and `approved_by` columns. `EmailSender.send_all()` should check `status == "APPROVED"` before proceeding.

---

### Step 30 — Per-Document Re-Extraction

**Goal:** Edit field map → re-extract single doc → verify. No need to re-run entire job.

**Prerequisites:** Document viewer (Step 26b) shows source page for verification. Field map editor (Step 21d) already exists.

| File | What to do |
|---|---|
| `app/api/routes/jobs.py` | `POST /jobs/{job_id}/extract/{doc_id}` — SSE stream for single doc. Accepts optional field_map override. |
| `app/pipeline/two_phase.py` | Extract per-doc logic into `_extract_single_document()` |
| `frontend/src/pages/ProjectDetail.tsx` | "Re-extract" button per document. Show progress inline. Replace old results atomically. |
| `tests/test_reextraction.py` | Single-doc re-extraction, field map override, result replacement |

---

### Step 31 — Manual Entity Merge/Split

**Goal:** Auditor links/unlinks notification subjects with rationale, logged in audit trail.

**Prerequisites:** Merge explanation (Step 26c) shows WHY records were merged. Document viewer (Step 26b) lets auditor see source pages to verify.

| File | What to do |
|---|---|
| `app/api/routes/subjects.py` | NEW — `POST /subjects/merge` (merge 2+ subjects), `POST /subjects/split` (split 1 subject by extraction groups) |
| `app/rra/deduplicator.py` | `merge_subjects()` and `split_subject()` methods. Update PersonEntity links. |
| `app/audit/audit_log.py` | Log merge/split with before/after state |
| `frontend/src/pages/SubjectDetail.tsx` | "Merge with..." (search + confirm) and "Split" (select extractions to separate) |
| `tests/test_manual_merge_split.py` | Merge 2→1, split 1→2, data preservation, audit trail |

---

## Phase 8 — Scale & Polish

---

### Step 32 — Prefect Orchestration & Parallel Extraction

**Goal:** Replace monolithic generator with Prefect tasks. Each doc extracts independently. Failures don't block others.

| File | What to do |
|---|---|
| `app/pipeline/dag.py` | Wire actual Prefect tasks. Per-document extraction as independent tasks. |
| `app/core/settings.py` | `max_parallel_extractions=4`, `extraction_timeout_minutes=30` |
| `tests/test_orchestration.py` | Parallel extraction, failure isolation, timeout |

---

### Step 33 — Multi-Matter Portfolio Dashboard

**Goal:** Firm-level view: "6 active matters, 2 past deadline, 12K subjects total."

| File | What to do |
|---|---|
| `app/api/routes/dashboard.py` | `GET /dashboard/portfolio` — aggregate across all projects |
| `frontend/src/pages/Dashboard.tsx` | Portfolio cards, charts (subjects by protocol, notification progress) |
| `tests/test_portfolio.py` | Aggregation across projects |

---

### Step 34 — Per-Project False Positive Deny List

**Goal:** "Ignore 'Washington' as PERSON in this project" — persists across re-runs.

| File | What to do |
|---|---|
| `app/db/models.py` | `ProjectDenyList` model: project_id, entity_type, value, reason, created_by |
| `app/pii/presidio_engine.py` | Accept deny_list, filter matches before returning |
| `app/api/routes/projects.py` | CRUD `/projects/{id}/deny-list` |
| `frontend/src/pages/ProjectDetail.tsx` | Deny list panel + "Add to deny list" on false positives |
| `tests/test_deny_list.py` | CRUD, filtering, persistence |

---

### Step 35 — PDF Report Export

**Goal:** Formatted PDF notification list + methodology for regulatory filings.

| File | What to do |
|---|---|
| `app/export/pdf_report.py` | NEW — WeasyPrint-rendered PDF with header/footer, page numbers, masked data tables |
| `app/api/routes/exports.py` | Add `format=pdf` parameter |
| `tests/test_pdf_report.py` | PDF generation, masked data, formatting |
