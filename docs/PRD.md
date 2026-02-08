# PRD — Offline PII Breach Analysis & Extraction System

## 1. Problem Statement

Organizations responding to data breaches must rapidly determine:
- what personal data was exposed,
- who was affected,
- and where the data appears,

across thousands of heterogeneous documents (PDFs, DOCX, images, tables, scanned files).

Existing approaches are:
- manual and slow,
- opaque and hard to audit,
- overly dependent on probabilistic ML/LLM outputs.

This system provides an **offline-capable, deterministic, auditable pipeline**
to discover, extract, group, and review PII from breach data at scale.

---

## 2. Goals

### Primary Goals
- Identify PII and non-PII across large document corpora
- Produce a **PII Inventory** with precise evidence (page, span, bounding box)
- Group PII into **affected individuals** where possible
- Support **human review and correction**
- Operate fully **offline / air-gapped**
- Ensure **deterministic, reproducible outputs**

### Non-Goals (MVP)
- Real-time ingestion
- Cloud-hosted inference
- Autonomous decision-making without human oversight
- Broad multilingual OCR beyond configured languages

---

## 3. Supported Inputs (MVP Scope)

### File Types
- PDF (text-based and scanned)
- DOCX
- Image files (PNG, JPG, TIFF)

### Content Types
- Plain text
- Tables
- Forms
- Mixed layouts (text + tables + images)

---

## 4. PII Taxonomy & Sensitivity

### PII Categories (configurable)
- Identity: name, alias
- Contact: email, phone, address
- Government IDs: SSN, PAN, passport, national IDs
- Financial: bank account, credit/debit cards
- Health-related identifiers
- Credentials (user IDs, usernames)

### Sensitivity Levels
- **High**: government IDs, financial, health
- **Medium**: full name + address
- **Low**: email, phone (context-dependent)

Each extracted PII item must include:
- type
- sensitivity
- confidence score
- evidence pointer

---

## 5. System Guarantees

### Determinism & Reproducibility
- Same input + same config = same output
- Core extraction is non-generative
- LLM outputs must be evidence-bound

### Explainability
Every extracted PII item must include:
- source document ID
- page number
- text span and/or bounding box
- detection method (regex / NER / LLM)
- rule or model version

### Privacy Guarantees
- No raw PII written to logs
- Configurable storage modes:
  - **Strict mode**: no raw PII persisted
  - **Investigation mode**: encrypted raw PII with retention limits

---

## 6. High-Level Architecture

The system is a **durable, stage-based pipeline**, not autonomous agents.

### Core Stages
1. Discovery
2. Parsing (text, OCR, layout)
3. Detection (confidence ladder)
4. Extraction & normalization
5. QA & validation
6. Human review
7. Learning loop (offline)

---

## 7. Detection Philosophy (Confidence Ladder)

### Tier 1 — Rules & Validators (Primary)
- Regex + checksum/format validators
- High precision, deterministic

### Tier 2 — Classical NER
- Names, addresses, organizations
- Used where regex is insufficient

### Tier 3 — Local LLM (Constrained, Backup Only)
Used only when:
- document type is ambiguous,
- OCR output is noisy,
- table structure is complex,
- grouping is unclear.

Constraints:
- local models only,
- minimal text snippets,
- must return evidence references,
- no hallucinated values.

---

## 8. Output Contracts

### PII Inventory
- Document → Page → PII Item
- Evidence-backed
- Export formats:
  - Excel
  - JSON

### Affected Individual Grouping
- Deterministic rules first
- Probabilistic links explicitly labeled

### Breach Report
- Document count
- Affected individual estimate
- PII distribution
- Confidence ranges
- Known gaps

---

## 9. Human Review Guarantees
- Reviewers see original evidence
- Corrections are tracked with provenance
- Disagreements are logged
- Review actions are auditable

---

## 10. Metrics of Success

### System Metrics
- Documents/hour
- OCR success rate
- Parse failure rate

### Extraction Metrics
- Precision/recall by PII type
- Confidence distribution
- Rule vs NER vs LLM contribution

### Review Metrics
- Review load
- Time per task
- Correction rate

### Investigation Metric
- Time to breach-ready report

---

## 11. Deployment Constraints
- Fully offline / air-gapped capable
- CPU-first, GPU optional
- Pluggable OCR and ML models
- Configuration-driven behavior

---

## 12. Scope Guardrails

The following are explicitly out of scope for MVP:
- Cloud inference
- Autonomous remediation decisions
- Black-box extraction without evidence
- Non-reproducible pipelines