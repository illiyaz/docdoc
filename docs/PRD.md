# PRD — Forentis AI: AI-Driven Breach Notification Platform

## 1. Problem Statement

Organizations responding to data breaches must rapidly:
- determine what personal data was exposed and where it appears,
- identify every unique individual affected across thousands of heterogeneous documents,
- and deliver legally compliant breach notifications to those individuals within strict regulatory deadlines (30–90 days depending on jurisdiction).

Existing approaches are:
- manual and slow — linear document review requiring specialized teams and expensive eDiscovery platforms,
- opaque and hard to audit — "the model decided" is not a defensible answer to a regulator,
- overly dependent on probabilistic ML/LLM outputs without evidence trails,
- incomplete — tools that detect PII but stop short of producing a notification-ready list,
- expensive — current per-document review models drive high cost for large breach datasets.

This system provides an **offline-capable, deterministic, auditable, end-to-end pipeline** that goes from raw breach data to delivered notifications — not just a list of PII hits.

---

## 2. Goals

### Primary Goals
- Identify PII, PHI, FERPA, SPI, and PPRA data elements across large document corpora (85+ data element types)
- Produce a **PII Inventory** with precise evidence (page, span, bounding box) for every finding
- Deduplicate and resolve records to **unique affected individuals** via Rational Relationship Analysis (RRA)
- Apply a counsel-approved **Protocol** to determine which individuals have a notification obligation under applicable law
- **Deliver breach notifications** via email or print-ready postal output within regulatory deadlines
- Support **human review and legal approval** before any notification is sent
- Operate fully **offline / air-gapped** — zero runtime network dependencies
- Ensure **deterministic, reproducible, auditable outputs** — every decision traceable to a specific rule

### Non-Goals (MVP)
- Real-time ingestion or streaming breach feeds
- Cloud-hosted inference
- Autonomous notification without human approval
- Broad multilingual OCR beyond configured languages
- Autonomous remediation beyond notification

---

## 3. Supported Inputs

### File Types
- PDF (text-based and scanned/image-only)
- DOCX (Microsoft Word)
- XLSX / XLS (Excel, multi-tab aware)
- CSV (chunked streaming)
- HTML / XML
- Email (.eml, .msg)
- Parquet / Avro
- Images (PNG, JPG, TIFF) — via PaddleOCR
- All other formats — Apache Tika fallback (self-hosted)

### Content Types
- Plain text, prose documents
- Tables (detected and extracted separately from prose)
- Forms (field + value pairs)
- Mixed layouts (text + tables + images on the same page)
- Scanned/image-only pages (OCR path)
- Multi-page documents with PII spanning page boundaries

---

## 4. PII/PHI/SPI Taxonomy & Sensitivity

### Data Element Categories

| Category | Elements | Regulatory Framework |
|---|---|---|
| **Identity** | Name, alias, DOB | GDPR, CCPA, PIPEDA |
| **Contact** | Email, phone, address | GDPR, CCPA, TCPA |
| **Government IDs — US** | SSN, EIN, Medicare MBI, driver license | HIPAA, CCPA |
| **Government IDs — India** | Aadhaar, PAN card, voter ID | DPDP, IT-Act |
| **Government IDs — UK** | National Insurance number, NHS number | UK-GDPR |
| **Government IDs — EU** | German Personalausweis, French INSEE, Spanish DNI, Italian Codice Fiscale | GDPR |
| **Government IDs — CA/AU** | SIN, TFN, Medicare (AU), ABN | PIPEDA, Privacy Act AU |
| **Financial** | Credit/debit card (Luhn-validated), IBAN, bank account + routing, sort code | PCI-DSS, GLBA |
| **PHI — Medical** | Medical Record Number, NPI, DEA number, HICN, health plan beneficiary | HIPAA, HITECH |
| **PHI — Clinical** | ICD-10 diagnosis codes (context-dependent), prescription numbers | HIPAA |
| **FERPA** | Student ID numbers, education records (column-header driven) | FERPA |
| **SPI** | Biometric identifiers, financial account + routing pairs | CCPA, GDPR |
| **PPRA** | Student survey response data (context-dependent) | PPRA |
| **Network** | IPv4, IPv6, GPS coordinates | GDPR, CCPA |

### Sensitivity Levels
- **Critical**: government IDs, PHI, financial accounts
- **High**: SSN, Aadhaar, PAN, NI number, Medicare
- **Medium**: full name + DOB, name + address combination
- **Low**: email, phone, IP address (context-dependent)

Each extracted item carries: type, sensitivity, confidence score, evidence pointer, geography, regulatory framework.

---

## 5. System Guarantees

### Determinism & Reproducibility
- Same input + same config + same Protocol = same output
- Core extraction is non-generative (regex + NER, not LLM)
- LLM assist (Phase 5 only) must return evidence references — no hallucinated values accepted

### Explainability
Every extracted PII item must include:
- source document ID and file path
- page number or sheet name
- character offsets (start, end) and bounding box
- detection method (layer_1_pattern / layer_2_context / layer_3_positional)
- specific pattern or model version used
- confidence score

"The model decided" is never an acceptable audit entry.

### Privacy Guarantees
- No raw PII written to logs, exceptions, stack traces, or API responses
- Configurable storage modes:
  - **Strict mode** (default): no raw PII persisted anywhere; SHA-256 hash only
  - **Investigation mode**: encrypted raw PII (Fernet minimum) with mandatory retention limit
- Frontend displays masked values only (e.g. `***-**-6789`)

### Notification Guarantees
- No notification sent without an `APPROVED` workflow status
- Every sent notification logged with timestamp, delivery status, and approver chain
- Notification content generated from jurisdiction-specific templates only

---

## 6. High-Level Architecture

The system is a **durable, stage-based pipeline** — eight Prefect tasks with typed inputs and outputs. Not autonomous agents.

### Pipeline Stages

| Stage | Task | Output |
|---|---|---|
| 1 | **Discovery** | DocumentCatalog — list of all files to process |
| 2 | **Parsing** | ExtractedBlock list — text + evidence from every page/cell |
| 3 | **Detection** | DetectionResult list — PII hits with confidence scores |
| 4 | **Normalization** | NormalizedRecord list — phone → E.164, address → standard, name → canonical |
| 5 | **RRA** | NotificationSubject list — one per unique individual |
| 6 | **QA & Validation** | Validated NotificationSubject list, QC metrics |
| 7 | **Human Review** | Approved NotificationList — human-signed-off, legally defensible |
| 8 | **Notification Delivery** | Email sent / print-ready letters generated |

Each stage produces a fully typed output that the next stage consumes. Stages never skip or share state outside their defined contract.

---

## 7. Detection Philosophy (Confidence Ladder)

### Tier 1 — Rules & Validators (Primary, ~85–90% of cases)
- Regex patterns with format validators (Luhn for cards, checksum for Aadhaar, etc.)
- Microsoft Presidio built-in recognizers
- High precision, fully deterministic, no model required

### Tier 2 — Classical NER (Context Classification)
- spaCy NER for names, addresses, organizations
- Context window classifier (100 chars surrounding match) for low-confidence Tier 1 hits
- Used when confidence < 0.75 from Tier 1
- Still deterministic at inference — pre-trained static models, no runtime training

### Tier 3 — Local LLM (Constrained, Phase 5 Only, Governance-Gated)
Used only for:
- Ambiguous-label tiebreaking after Tier 1 and 2 disagree
- Never on the primary extraction path
- Never replacing the deterministic pipeline

Hard constraints:
- Local models only (Ollama: Llama 3 / Phi-3) — no cloud LLM APIs ever
- Minimal text snippets — no full document context sent to model
- Must return evidence references — bare classifications rejected
- Every LLM call logged: prompt + response + final decision
- Gated behind `llm_assist_enabled: false` in config.yaml

---

## 8. Rational Relationship Analysis (RRA)

RRA answers "who needs to be notified?" — the commercial core of the product.

### What it does
Links PII extractions across documents to a single unique individual. Produces one `NotificationSubject` per unique person in the breach dataset.

### Matching signals (strongest → weakest)

| Signal | Confidence contribution |
|---|---|
| SSN / government ID exact match | +0.50 |
| Exact email match | +0.40 |
| Exact phone match (normalized) | +0.35 |
| Name + DOB match | +0.35 |
| Name + address (fuzzy) match | +0.25 |
| Name only (soundex) | +0.10 |

- Combined confidence ≥ 0.80 → merge accepted automatically
- Combined confidence 0.60–0.79 → surfaces in RRA human review queue
- Combined confidence < 0.60 → kept as separate subjects

### NotificationSubject
One per unique individual. Carries: canonical name, email, address, phone, all PII types found, source record UUIDs, merge confidence, notification obligation flag, workflow status.

---

## 9. Protocol Configuration

A Protocol defines the notification obligation for a given engagement. Selected once at job creation, pre-approved by counsel, never changed mid-job.

### Built-in Protocols

| Protocol | Framework | Key triggers | Deadline |
|---|---|---|---|
| `hipaa_breach_rule` | HIPAA | PHI exposure | 60 days |
| `gdpr_article_33` | EU GDPR | Any personal data of EU residents | 72 hours (authority) / 30 days (individuals) |
| `ccpa` | California | SSN, financial, medical, biometric | 45 days |
| `hitech` | HITECH | PHI, stricter than HIPAA | 60 days |
| `ferpa` | FERPA | Student education records | No fixed deadline |
| `state_breach_generic` | US State laws | SSN + name; financial + name | 30–90 days (varies) |

Custom protocols defined in `config/protocols/*.yaml`.

---

## 10. Human Review & Approval Workflow

Every notification requires a human approval chain. This is the legal defensibility layer.

### Roles

| Role | Responsibility |
|---|---|
| `REVIEWER` | Reviews individual AI-flagged records; approves or rejects |
| `LEGAL_REVIEWER` | Applies regulatory judgment to reviewer-escalated edge cases |
| `APPROVER` | Final sign-off before notification list is generated |
| `QC_SAMPLER` | Random sampling validation (5–10% of AI-approved records) |

### Workflow States
```
AI_PENDING → HUMAN_REVIEW → LEGAL_REVIEW (if escalated) → APPROVED → NOTIFIED
                           ↘ REJECTED
```

### Review Queues (four)
1. **Low-confidence queue** — AI extractions with score < 0.75
2. **Escalation queue** — records flagged by Reviewer needing regulatory judgment
3. **QC sampling queue** — 5–10% random sample of AI-approved records
4. **RRA review queue** — entity merges with confidence 0.60–0.79

### Hard rules
- Every human decision requires a non-empty `rationale` field
- `LEGAL_REVIEWER` decisions require a `regulatory_basis` citation
- All corrections captured as labeled training data for model improvement
- Audit trail records every state transition with actor, timestamp, rationale

---

## 11. Notification Delivery

### Email Delivery
- SMTP only — local relay, no cloud email APIs
- Jurisdiction-specific HTML templates per protocol
- Rate limit: 100 emails/minute
- Retry: 3 attempts with exponential backoff → `DELIVERY_FAILED`
- Delivery receipt logged: subject_id, timestamp, SMTP response

### Print-Ready Output
- One PDF letter per recipient (WeasyPrint)
- Output: `/output/notifications/{job_id}/letters/{subject_id}_letter.pdf`
- Manifest: `manifest.csv` — subject_id, name, address, letter filename
- Letters contain only minimum required fields per protocol

---

## 12. Output Contracts

### PII Inventory
- Document → Page/Sheet → PII Item → Evidence
- Every item: type, sensitivity, confidence, page, bbox, span, pattern used, geography, regulatory framework
- Export: Excel, JSON

### NotificationSubject List
- One row per unique individual
- Fields: canonical name, contact info, all PII types found, source documents, merge confidence
- Export: Excel, CSV, JSON

### NotificationList
- Protocol-filtered NotificationSubjects with notification_required = true
- Includes: delivery method (email / print), status, delivery timestamp

### Breach Report
- Document count, affected individual count
- PII type distribution with sensitivity breakdown
- Confidence score distribution
- Regulatory frameworks implicated
- Notification deadline tracking per protocol
- Known gaps and low-confidence extractions flagged for review
- Export: PDF, Excel

### Audit Log
- Append-only, immutable
- Every AI extraction event, human review event, workflow state change, notification send
- Export: JSON (structured for regulatory submission)

---

## 13. Metrics of Success

### System Metrics
- Documents processed per hour
- OCR success rate by page type (digital / scanned / corrupted)
- Parse failure rate by file type

### Extraction Metrics
- Precision / recall by PII type and geography
- Confidence score distribution (Tier 1 vs 2 vs 3 contribution)
- False positive rate (flagged by human review)
- Cross-page entity capture rate (stitching effectiveness)

### RRA Metrics
- Deduplication ratio (raw records → unique subjects)
- RRA auto-accept rate (confidence ≥ 0.80)
- Human merge confirmation rate

### Review Metrics
- Review queue depth by type
- Time per review task by role
- Human correction rate (AI accuracy proxy)
- Escalation rate (Reviewer → Legal)

### Notification Metrics
- Email delivery success rate
- Time from job start to notification sent
- Regulatory deadline compliance rate

### Cost Metrics
- Cost per document processed
- Cost per notification sent
- Reduction vs. traditional manual review baseline

---

## 14. Deployment Modes

### Air-Gap Mode (primary)
- All models, libraries, and dependencies pre-staged locally
- Zero outbound network calls at runtime
- SMTP via local mail relay only
- HashiCorp Vault (self-hosted) for key management

### Cloud VPC Mode
- Same codebase, deployed to GCP or Azure private VPC
- Internal-only networking — no public egress
- S3/GCS connectors via MinIO-compatible API
- Functionally equivalent to air-gap: no cloud inference, no cloud APIs

### Common constraints (both modes)
- CPU-first; GPU optional for PaddleOCR acceleration
- Pluggable OCR and ML models
- Configuration-driven behavior — no code changes between deployments

---

## 15. Scope Guardrails

Out of scope for all phases:
- Cloud LLM inference (OpenAI, Anthropic API, Cohere, etc.)
- Cloud email delivery APIs (SendGrid, Mailgun, etc.)
- Autonomous notification without human approval
- Black-box extraction without evidence trail
- Non-reproducible pipelines
- Autonomous merge decisions below 0.80 RRA confidence (always human-confirmed)
- Remediation actions beyond notification