# Offline PII Breach Analysis & Extraction System

An **offline, audit-grade system** to discover, extract, group, and review
Personally Identifiable Information (PII) from large document corpora
during breach investigations.

This system is designed for **determinism, explainability, and legal defensibility** —
not demo-grade extraction or opaque AI behavior.

---

## 🎯 Problem This Solves

During breach response, investigators must answer:
- What PII was exposed?
- Where does it appear?
- How many individuals are affected?
- How confident are we?

Source data typically consists of **thousands of PDFs, DOCX files, scans, and tables**,
often with inconsistent layouts and partial structure.

This system provides a **reproducible pipeline** that produces:
- an evidence-backed **PII Inventory**
- an estimate of **affected individuals**
- a defensible **breach report**

---

## 🧭 Design Principles

- **Offline / Air-gapped first**
- **Rules + validators first**, ML/LLM only as constrained assist
- **Deterministic outputs** (same input → same result)
- **Evidence-backed extraction** (page, span, bounding box)
- **Human-in-the-loop by design**
- **Auditability over cleverness**

---

## 🏗️ High-Level Architecture

The system is a **durable, stage-based pipeline**:

1. **Discovery**
   - Enumerate files
   - Hash + deduplicate
2. **Parsing**
   - PDF text extraction
   - OCR for scanned pages
   - Layout awareness
3. **Page Triage**
   - Detect where meaningful content begins
   - Skip boilerplate pages by default
4. **Detection (Confidence Ladder)**
   - Tier 1: Regex + validators (primary)
   - Tier 2: Classical NER
   - Tier 3: Local LLM (backup only)
5. **Extraction & Normalization**
   - Canonical formatting
   - Hashing / tokenization
6. **QA & Validation**
   - Deduplication
   - Sanity checks
7. **Person Grouping**
   - Group PII into affected individuals
8. **Human Review**
   - Evidence-based corrections
9. **Export & Reporting**
   - Inventory, summaries, audit trails

---

## 🧠 Detection Philosophy (Very Important)

This is **not an LLM-first system**.

### Default approach
- **Regex + checksum/format validators**
- Deterministic, explainable, fast

### Used when needed
- **Classical NER** for names/addresses
- **Local LLMs** only for:
  - ambiguous structure
  - messy OCR
  - table interpretation
  - grouping disambiguation

### Hard constraints on LLM usage
- Local models only
- Minimal text snippets
- Must reference evidence (offsets/bboxes)
- No hallucinated values

---

## 🔐 Privacy & Security Model

### Storage modes
- **Strict mode (default)**
  - No raw PII persisted
  - Only hashed or masked values stored
- **Investigation mode**
  - Encrypted raw PII
  - Explicit retention limits
  - Full audit logging

### Guarantees
- No raw PII in logs
- All sensitive access is audited
- Deterministic hashing for dedupe and grouping

---

## 📦 Outputs

### 1. PII Inventory
- One row per PII item
- Includes:
  - document
  - page
  - offsets / bounding boxes
  - detection method
  - confidence
  - sensitivity
- Export formats:
  - Excel
  - JSON

### 2. Affected Individual Grouping
- Deterministic grouping where possible
- Probabilistic links explicitly labeled

### 3. Breach Report
- Document counts
- Estimated affected individuals
- PII type distribution
- Confidence ranges
- Known gaps and uncertainties

---

## 🗂️ Repository Structure (Simplified)
app/
api/              # FastAPI endpoints
pipeline/         # Orchestrated stages
parsing/          # PDF, DOCX, OCR, layout
pii/              # Rules, validators, NER, LLM assist
review/           # Human review workflow
exports/          # Excel / JSON / reports
db/               # Models, repos, migrations
core/             # Config, policies, security
docs/
PRD.md            # Single source of truth
reference/        # Historical design notes
tests/

---

## 🚀 Getting Started (Local, Offline)

### Requirements
- Python 3.11+
- Postgres (local)
- No internet access required

### Local setup
```bash
docker-compose up -d
uvicorn app.main:app --reload

Ingest a folder

POST /v1/ingest/folder
{
  "path": "/data/breach_files",
  "mode": "strict"
}

Export results

GET /v1/export/{run_id}?format=xlsx

📜 Source of Truth
   •  docs/PRD.md is the single source of truth
   •  If code, prompts, or assumptions conflict with PRD.md → PRD.md wins
   •  Reference docs exist only for historical context

⸻

🚧 Explicit Non-Goals (MVP)
   •  Cloud inference
   •  Real-time streaming ingestion
   •  Autonomous remediation
   •  Black-box extraction without evidence
   •  Non-reproducible pipelines
   