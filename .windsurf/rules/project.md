# Windsurf Cascade Rules — Cyber NotifAI (AI-Driven Breach Notification Platform)

Read CLAUDE.md at the project root for full architectural detail.
This file contains the guardrails Cascade must follow at all times.

---

## What This Product Is

An end-to-end breach notification platform with three outcomes:
1. **Identify** — extract PII/PHI/FERPA/SPI from every document in a breach dataset
2. **Resolve** — deduplicate and link records to unique individuals (Rational Relationship Analysis)
3. **Notify** — generate and deliver breach notifications per counsel-approved Protocol

A deterministic, air-gap-safe pipeline. Eight Prefect tasks (not agents).
No cloud APIs. No LLM at runtime unless explicitly enabled via config flag.

---

## Hard Technology Rules — Never Violate

### Use these:
- PDF processing: **PyMuPDF (fitz)** only — page-by-page streaming, never full load
- OCR: **PaddleOCR** only
- PII detection: **Microsoft Presidio + spaCy** only
- Orchestration: **Prefect** only
- Backend: **FastAPI**
- Frontend: **React + Tailwind + ShadCN**
- ORM: **SQLAlchemy** with **Alembic** migrations
- Storage: **PostgreSQL + Redis + MinIO**
- Key management: **HashiCorp Vault (self-hosted)**
- Excel (.xlsx, .xls): **openpyxl** — multi-tab aware, streaming mode only
- HTML / email (.eml): **beautifulsoup4** + stdlib email
- Parquet / Avro: **pyarrow** + pandas
- Multi-format fallback: **Apache Tika** (self-hosted)

### Never introduce:
- ❌ LangGraph, CrewAI, AutoGen, or any agent framework
- ❌ Cloud LLM APIs (OpenAI, Anthropic API, Cohere, etc.)
- ❌ pdfplumber or PyPDF2
- ❌ Tesseract OCR
- ❌ Any library with runtime network calls
- ❌ Airflow (use Prefect)

---

## Project Structure — Never Deviate

```
app/
  tasks/          # eight Prefect pipeline stages
  pipeline/       # Prefect DAG wiring
  readers/        # all file type readers — PDF, Excel, DOCX, CSV, HTML, email, etc.
  pii/            # Presidio engine, spaCy classifier, 3-layer extraction
  normalization/  # phone/address/name/email normalizers
  rra/            # entity resolution, deduplication, NotificationSubject
  protocols/      # Protocol configs and regulatory threshold engine
  notification/   # list builder, email sender, print renderer, templates
  core/           # policies.py, security.py, audit.py
  db/             # models.py, repositories.py, base.py, session.py
  api/            # FastAPI routes
frontend/         # React human review UI (4 queues, multi-role)
models/           # pre-packaged spaCy + Presidio models (air-gap artifact)
config/
  config.yaml
  protocols/      # YAML protocol definitions (one per regulatory framework)
output/
  notifications/  # generated letter PDFs and email manifests
tests/
alembic/
```

### Readers folder breakdown (`app/readers/`)

```
app/readers/
  base.py           # ExtractedBlock dataclass — canonical output for ALL readers
  pdf_reader.py     # PyMuPDF streaming + dual-path (digital/scanned)
  ocr.py            # PaddleOCR integration
  onset.py          # content onset detection (skip cover pages/TOC)
  stitcher.py       # cross-page tail-buffer logic
  excel_reader.py   # openpyxl multi-tab reader
  docx_reader.py    # python-docx
  csv_reader.py     # pandas
  html_reader.py    # beautifulsoup4
  email_reader.py   # stdlib email + beautifulsoup4
  parquet_reader.py # pyarrow + pandas
  tika_reader.py    # Apache Tika fallback for unsupported formats
  registry.py       # maps file extension → correct reader class
```

Every reader must output `ExtractedBlock` objects. Downstream stages never know
what file type they're processing — they only see `ExtractedBlock` lists.

---

## Rational Relationship Analysis (RRA) Rules

- RRA runs after normalization, never before
- RRA never modifies source PII records — creates links only
- Merge accepted at confidence ≥ 0.80; 0.60–0.79 goes to human review queue
- Every merge decision written to audit trail
- Output: one `NotificationSubject` per unique individual in breach dataset

## Protocol Rules

- Protocol selected once per job at creation time — never changed mid-job
- Notification task rejects any job with no Protocol assigned
- Custom protocols in `config/protocols/*.yaml` — never hardcoded
- Built-in protocols: `hipaa_breach_rule`, `gdpr_article_33`, `ccpa`, `hitech`, `ferpa`, `state_breach_generic`

## Notification Delivery Rules

- Delivery gated on `APPROVED` workflow status only
- Email: SMTP local relay only — no cloud email APIs (SendGrid, Mailgun prohibited)
- Print: WeasyPrint PDF — no cloud rendering
- Notification content from templates only — never raw LLM output
- Every send logged in audit trail

## Human Review Workflow Rules

Four queues, four roles:
- `REVIEWER` → reviews individual PII records
- `LEGAL_REVIEWER` → applies regulatory judgment to escalations  
- `APPROVER` → final sign-off before notification
- `QC_SAMPLER` → 5-10% random sampling validation

Workflow states: `AI_PENDING → HUMAN_REVIEW → (LEGAL_REVIEW) → APPROVED → NOTIFIED`
Every human decision requires a `rationale` string — blank rationale is a validation error.



### All readers
- Every reader outputs `ExtractedBlock` objects — never raw text strings
- `ExtractedBlock` must carry: `text`, `page_or_sheet`, `bbox`, `row`, `column`, `source_path`, `file_type`
- `bbox` is None for non-visual formats (CSV, JSON, etc.) — that is expected and valid
- Reader registry in `app/readers/registry.py` maps file extension → reader class
- Apache Tika is the fallback only — always prefer the specific reader for known formats

### PDF rules
- Always stream page-by-page: `doc.load_page(n)` then `doc._forget_page(n)`
- Never load the entire PDF into memory
- Classify every page before processing: `digital | scanned | corrupted`
- Scanned/corrupted pages → PaddleOCR, not Tesseract
- Run onset detection before extraction — skip cover pages and TOC
- Coordinates are evidence only — never use bbox as the search mechanism
- Always prepend tail buffer (last 5 lines) of previous page before extracting

### Excel rules
- Use `openpyxl.load_workbook(read_only=True, data_only=True)` always — never eager load
- Preserve tab name as `page_or_sheet` on every `ExtractedBlock` — never flatten tabs
- Preserve `cell.row` and `cell.column` as provenance evidence
- False positive guard: if a numeric pattern (e.g. `XXX-XX-XXXX`) appears in >80% of
  cells in the same column, treat it as a structured ID field, not PII — flag for
  human review rather than auto-extracting
- Column headers are critical context — always read row 1 of each sheet as header
  metadata and attach to blocks in that column (feeds Layer 3 positional inference)

### Other format rules
- CSV: use pandas `chunksize` iterator — never `read_csv()` on full file at once
- HTML/email: strip all tags before extraction; preserve original structure as metadata
- Parquet/Avro: use pyarrow streaming reader, process row-group by row-group

---

## PII Extraction Rules

Three layers, first match wins:
1. **Layer 1** — Presidio + regex (no label needed, ~85-90% of cases)
2. **Layer 2** — spaCy context window classifier (for confidence < 0.75)
3. **Layer 3** — positional/header inference (tabular data, never used alone)

Every extracted value must carry: `page_or_sheet`, `bbox`, `row`, `column`, `extraction_layer`, `pattern_used`, `confidence`, `hashed_value`.

---

## Storage Policy — Absolute Rules

**STRICT mode (default):**
- Never store `raw_value` anywhere — not DB, not logs, not exceptions
- `hashed_value` required: `SHA256(tenant_salt + raw_value)`
- `raw_value_encrypted` must be NULL

**INVESTIGATION mode:**
- `raw_value_encrypted` allowed (Fernet minimum)
- `retention_until` required and enforced
- `hashed_value` still required
- Fail closed if encryption key is missing — never fall back to plaintext

---

## Safety Rules — Non-Negotiable

- Raw PII never appears in logs, exceptions, stack traces, debug output, or API responses
- Frontend displays masked values only (e.g. `***-**-6789`) — never raw PII
- LLM assist is off by default: `llm_assist_enabled: false` in config.yaml
- All runtime dependencies must work fully offline (air-gap safe)

---

## Schema Change Rules

Any new table or column requires simultaneous updates to:
1. `app/db/models.py`
2. `alembic/versions/0001_initial.py`
3. `tests/test_schema.py`
4. Any affected repository tests

Never update one without the others.

---

## Code Style Rules

- Business logic lives in `app/tasks/` and `app/pii/` — never in DB models
- Repositories are thin — only data access, no business logic
- Policy decisions belong in `app/core/policies.py`
- Crypto belongs in `app/core/security.py`
- Keep every change small and coherent — one behavior per edit
- Add tests in the same change for every behavior modification
- Never broaden scope beyond the current prompt

---

## Active Phase: Phase 1 Only

Do not implement Phase 2, 3, 4, or 5 features. Current scope:
- Storage policy + security foundation ✅
- ExtractedBlock + reader registry ✅
- PDF reader (PyMuPDF streaming + classifier) ✅
- PaddleOCR integration ✅
- Onset detection + tail-buffer stitching ✅
- Excel reader (openpyxl multi-tab) ✅
- Presidio + spaCy Layer 1 extraction — **including PHI/FERPA/SPI/PPRA patterns**
- PostgreSQL checkpointing
- Discovery task (filesystem + PostgreSQL connectors)
- FastAPI skeleton

**Phase 1 gate before moving to Phase 2:**
All 85+ patterns implemented (PII + PHI + FERPA + SPI + geography patterns).
Full test suite green. Checkpointing works. FastAPI returns extraction results.

## Phase Roadmap Summary

| Phase | Focus | Key output |
|---|---|---|
| 1 (active) | Deterministic extraction | PII hits per document |
| 2 | Normalization + RRA | NotificationSubject list (deduplicated individuals) |
| 3 | Protocol + Notification | Emails sent / letters printed |
| 4 | Enhanced HITL + Audit | Legally defensible approval chain |
| 5 | Local LLM (gated) | Tiebreaking for ambiguous cases |
