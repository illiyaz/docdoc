# Technical Architecture Reference

Detailed specifications for PDF processing, PII detection, RRA, protocols, HITL workflow, and notification delivery. See [CLAUDE.md](../CLAUDE.md) for project overview and conventions.

---

## PDF Processing Architecture (Locked — Do Not Change)

These decisions are final. Do not introduce alternative libraries without explicit approval.

### Primary PDF engine: PyMuPDF (fitz)

- **Always** use `fitz.open()` with page-by-page streaming
- **Never** load entire documents into memory
- Call `doc._forget_page(page_num)` after processing each page
- Use `page.get_text("dict")` for structured blocks with bounding boxes

```python
import fitz

doc = fitz.open("document.pdf")
for page_num in range(len(doc)):
    page = doc.load_page(page_num)
    blocks = page.get_text("dict")
    process_page(page_num, blocks)
    doc._forget_page(page_num)  # required — release memory immediately
```

### Dual-path pipeline: digital vs. scanned

Every page must be classified before processing:

```python
def classify_page(page) -> str:
    word_count = len(page.get_text().split())
    if word_count > 50:
        return "digital"    # real text layer — use PyMuPDF directly
    elif word_count > 5:
        return "corrupted"  # bad OCR layer — re-OCR with PaddleOCR
    else:
        return "scanned"    # pure image — must OCR with PaddleOCR
```

- **Digital path:** PyMuPDF → text + coordinates directly
- **Scanned/corrupted path:** PyMuPDF render page as image → PaddleOCR → text + coordinates

Both paths produce identical normalized output. Downstream stages must not care which path was taken.

### OCR engine: PaddleOCR (not Tesseract)

PaddleOCR is chosen over Tesseract for:
- Superior accuracy on degraded, skewed, and low-resolution scans
- Per-word bounding boxes (required for evidence tracking)
- Better handling of mixed-layout documents

Tesseract must not be introduced as an alternative.

### Content onset detection (mandatory pre-pass)

Before extraction begins, detect the page where real data starts. Do not process cover pages, tables of contents, or legal disclaimers.

```python
ONSET_SIGNALS = [
    r'\b(name|ssn|date of birth|dob|address|account|policy)\b',
    r'\d{3}-\d{2}-\d{4}',   # SSN
    r'\b[A-Z]{2}\d{6,}\b',  # ID numbers
]

def find_data_onset(doc) -> int:
    for page_num in range(len(doc)):
        text = doc.load_page(page_num).get_text()
        if any(re.search(sig, text, re.I) for sig in ONSET_SIGNALS):
            return max(0, page_num - 1)  # start one page before first signal
    return 0
```

Store `onset_page` in the DocumentCatalog record. The extraction pipeline starts from `onset_page`, never from page 0 by default.

### Coordinate handling: evidence-only (critical rule)

**Coordinates are recorded as evidence. They are never used as the search mechanism.**

```
❌ Wrong: "SSN lives at coordinate (120, 340) on every page — go fetch it"
✅ Right: "Found SSN via pattern match — record its bbox as provenance evidence"
```

Semantic block grouping uses a y-coordinate tolerance to handle layout variance across pages:

```python
def group_blocks_by_reading_order(blocks):
    return sorted(
        blocks,
        key=lambda b: (round(b["bbox"][1] / 20), b["bbox"][0])
        #               ↑ row grouping with 20px tolerance, then left-to-right
    )
```

### Cross-page entity stitching (tail-buffer pattern)

For data that spans page boundaries, maintain a tail buffer of the last 5 lines of each page and prepend to the next page before extraction:

```python
class PageProcessor:
    def __init__(self):
        self.tail_buffer: list[str] = []

    def process_page(self, page_num, blocks):
        stitched = "\n".join(self.tail_buffer) + "\n" + blocks_to_text(blocks)
        results = extract_pii(stitched)

        for r in results:
            if r.start_char < len("\n".join(self.tail_buffer)):
                r.spans_pages = (page_num - 1, page_num)

        self.tail_buffer = get_last_n_lines(blocks_to_text(blocks), n=5)
        return results
```

All cross-page extractions must be flagged with `spans_pages` in the extraction output.

### Checkpointing (mandatory)

After processing each page, write a checkpoint:

```python
{
    "document_id": "uuid",
    "last_completed_page": 847,
    "onset_page": 4,
    "partial_results": [...],
    "checkpoint_timestamp": "iso8601"
}
```

Failed jobs resume from `last_completed_page + 1`. Never reprocess from page 0.

---

## PII Detection & Extraction Architecture (Locked)

### Extraction layer model: three layers, first match wins

**Layer 1 — Pattern Match (primary, no label needed):**
Presidio built-in recognizers + custom regex. Handles SSN, email, phone, credit card, IP address without requiring labels. This resolves ~85–90% of PII.

**Layer 2 — Context Window Classification:**
For Presidio results with confidence < 0.75, examine the 100 characters surrounding the match and apply a spaCy text classifier to infer PII type from context. Handles missing-label cases.

**Layer 3 — Positional/Header Inference:**
For tabular data, infer PII type from the nearest column header (scanning upward). Used only when Layer 1 or Layer 2 provides corroborating confidence. Never used in isolation.

### PII detection libraries

| Library | Role | Air-gap safe |
|---|---|---|
| Microsoft Presidio | Primary PII detection + anonymization | Yes |
| spaCy (en_core_web_trf) | NER + context classification | Yes (model ships with artifact) |
| Custom regex recognizers | Domain-specific PII patterns | Yes |

HuggingFace Hub may be used at training time only. Never called at inference time.

### ML role (current and future)

**Now (Phase 1–2):** No ML at inference time beyond spaCy NER. Presidio + spaCy models are pre-trained, packaged, and static.

**Phase 2:** Fine-tune spaCy NER on labeled data from human review corrections. Still deterministic at inference. Still fully local.

**Phase 3:** Custom sklearn/spaCy classifiers for domain-specific PII patterns, trained on accumulated human-corrected labels. Tracked in MLflow (self-hosted).

**Phase 4 (if governance approved):** Local LLM via Ollama (Llama 3 / Phi-3) for ambiguous-label tiebreaking only. Gated behind `llm_assist_enabled: false`. Never on core extraction path. Every LLM call logged with full input/output for audit.

**What "reinforcement learning" actually means in this system:** Supervised retraining on human-corrected labels. Not a neural RL loop. Retraining is weekly/monthly batch, not online.

---

## Expanded PII Pattern Scope

Patterns must cover PII, PHI, FERPA, SPI, and PPRA. All in `app/pii/layer1_patterns.py`.

### PHI (HIPAA Protected Health Information)
- Medical Record Number (MRN), Health Insurance Claim Number (HICN)
- NPI (National Provider Identifier): 10-digit
- DEA prescriber number: format AX9999999
- Health plan beneficiary number
- ICD-10 diagnosis codes: score 0.60 (needs Layer 2 context)

### FERPA (student education records)
- Student ID — flagged via column header "student id" / "student number", score 0.65

### SPI (Sensitive Personal Information)
- Biometric identifiers (context keyword driven)
- Financial account + routing number pairs
- All PHI types are also SPI

### PPRA (Protection of Pupil Rights)
- Student survey response data, column header driven, score 0.60

All patterns carry `geography` and `regulatory_framework` fields.
`get_all_patterns(geographies=None)` filters by geography — GLOBAL always included.

---

## Human-in-the-Loop Workflow

The human review system is not a simple approval queue — it is the legal defensibility layer. Every notification that goes out must have a traceable human approval chain.

### Reviewer Roles

| Role | What they do |
|---|---|
| `REVIEWER` | Reviews AI-flagged records, approves/rejects individual PII findings |
| `LEGAL_REVIEWER` | Applies regulatory judgment to edge cases escalated by reviewers |
| `APPROVER` | Final sign-off before notification list is generated and delivered |
| `QC_SAMPLER` | Runs random sampling validation on AI-approved output |

### Workflow States (per NotificationSubject)

```
AI_PENDING → HUMAN_REVIEW → LEGAL_REVIEW (if escalated) → APPROVED → NOTIFIED
                          ↘ REJECTED
```

Every state transition is recorded in the audit trail with actor, timestamp, and rationale.

### Review Queues (four, not two)

1. **Low-confidence queue** — AI extractions with score < 0.75 (Layer 2 needed)
2. **Escalation queue** — records flagged by Reviewer as needing regulatory judgment
3. **QC sampling queue** — 5–10% random sample of all AI-approved records for quality validation
4. **RRA review queue** — entity merges where RRA confidence < 0.80 (two records linked but uncertain)

### Hard rules

- **Never display raw PII** — only masked or normalized values (e.g., `***-**-6789`)
- **Never send raw PII to the frontend API** — mask at the API layer before response
- Every human decision requires a `rationale` string — empty rationale is a validation error
- `LEGAL_REVIEWER` decisions require a `regulatory_basis` field (e.g., "HIPAA §164.400 — applies")
- All reviewer corrections are captured as labeled training data for Phase 2 model retraining
- Review queue priority: ascending confidence score (least certain first)

---

## Multi-Data-Source Connector Design

The Discovery task must use a pluggable connector interface. Each data source type is a separate connector:

```python
class DataSourceConnector(ABC):
    @abstractmethod
    def list_documents(self) -> list[DocumentCatalog]: ...
    @abstractmethod
    def fetch_document(self, doc_id: str) -> bytes: ...
```

Supported connectors (implement in order):
1. Local filesystem
2. PostgreSQL (table → row → document)
3. MinIO / S3
4. MongoDB
5. Email (IMAP, air-gap safe)

Each connector ships independently. The pipeline does not know or care which connector sourced a document.

---

## Rational Relationship Analysis (RRA)

RRA is the core differentiator of the product. It answers "who needs to be notified?" not just "what PII was found?"

### What RRA does

Links PII extractions across documents to a single unique individual. Two records are the same person if they share enough corroborating fields. The output is a `NotificationSubject` — one per unique individual in the breach dataset.

### Matching hierarchy (strongest to weakest)

| Signal | Confidence boost |
|---|---|
| SSN / government ID match | +0.50 |
| Exact email match | +0.40 |
| Exact phone (normalized) match | +0.35 |
| Name + DOB match | +0.35 |
| Name + address (fuzzy) match | +0.25 |
| Name only (fuzzy soundex) | +0.10 |

A merge is **accepted** at combined confidence >= 0.80. Merges 0.60–0.79 go to the **RRA review queue** for human confirmation. Below 0.60 are kept as separate subjects.

### NotificationSubject dataclass

```python
@dataclass
class NotificationSubject:
    subject_id: str                    # UUID, stable across updates
    canonical_name: str                # normalized best-match name
    canonical_email: str | None        # primary notification email
    canonical_address: dict | None     # street, city, state, zip
    canonical_phone: str | None        # E.164
    pii_types_found: list[str]         # all entity types found across all records
    source_records: list[str]          # PIIRecord UUIDs that were merged
    merge_confidence: float
    notification_required: bool        # set by protocol engine
    review_status: str                 # workflow state
```

### RRA package structure

```
app/rra/
  entity_resolver.py       # links records by matching signals
  deduplicator.py          # merges duplicate NotificationSubjects
  notification_subject.py  # NotificationSubject dataclass + DB model
  fuzzy.py                 # soundex, jaro-winkler helpers
```

### Rules

- RRA runs after normalization, never before
- RRA never modifies source PII records — it only creates links between them
- Low-confidence merges surface in the human review queue, not auto-accepted
- Every merge decision is written to the audit trail

---

## Protocol Configuration

A Protocol defines what triggers a notification obligation for a given engagement.

### Protocol structure

```python
@dataclass
class Protocol:
    protocol_id: str
    name: str
    jurisdiction: str
    triggering_entity_types: list[str]
    notification_threshold: int
    notification_deadline_days: int
    required_notification_content: list[str]
    regulatory_framework: str
```

### Built-in protocols

| Protocol ID | Framework | Key triggers |
|---|---|---|
| `hipaa_breach_rule` | HIPAA | PHI: medical records, health insurance, SSN+DOB |
| `gdpr_article_33` | EU GDPR | Any personal data of EU residents |
| `ccpa` | California | SSN, financial account, medical, biometric |
| `hitech` | HITECH | PHI, stricter timelines than HIPAA |
| `ferpa` | FERPA | Student education records |
| `state_breach_generic` | US States | SSN+name; financial+name combinations |

### Rules

- Protocol is selected once per job, stored in `JobCatalog`, never changed mid-job
- Notification task must reject any job with no Protocol assigned
- Custom protocols live in `config/protocols/*.yaml` — never in code
- Protocol changes require a new job — no retroactive changes on a running job

---

## Notification Delivery

The final pipeline stage. Takes a human-approved `NotificationList` and delivers it.

### Delivery modes

**Email:** SMTP only — local relay, no cloud email APIs. Rate limit: 100/minute.
Templates: `app/notification/templates/{protocol_id}_email.html`
Retry: 3 attempts with exponential backoff → `DELIVERY_FAILED`

**Print-ready:** WeasyPrint PDF per recipient.
Output: `/output/notifications/{job_id}/letters/{subject_id}_letter.pdf`
Manifest: `manifest.csv` — subject_id, name, address, letter filename

### Hard rules

- Delivery gated on `APPROVED` status only — never send from `HUMAN_REVIEW`
- Every send logged in audit trail with timestamp and delivery status
- Notification content from templates only — never raw LLM output
- Email addresses come only from data found in the breach dataset
- Letters contain only minimum required fields per protocol

### Notification package structure

```
app/notification/
  list_builder.py         # applies protocol, produces NotificationList
  email_sender.py         # SMTP delivery
  print_renderer.py       # WeasyPrint PDF generation
  regulatory_threshold.py # does this subject's PII meet protocol threshold?
  templates/              # one HTML pair (email + letter) per protocol
```

---

## Storage Policy Contract

Implement in `app/core/policies.py` and `app/core/security.py`.

### STRICT mode (default)

- Never store `raw_value` anywhere — not in DB, not in logs, not in exceptions
- `hashed_value` is required for every extracted PII element
- `raw_value_encrypted` must be NULL
- `normalized_value` may be masked (configurable per field)
- Default storage policy: `hash`

### INVESTIGATION mode

- `raw_value_encrypted` is allowed (encrypted at rest via Fernet, minimum)
- `retention_until` is required and enforced — records auto-expire
- `hashed_value` still required
- Decryption must work via pluggable `EncryptionProvider` interface
- Vault-managed keys only — never hardcoded or environment-variable keys in INVESTIGATION mode

### Security implementation (`app/core/security.py`)

- Hashing: `SHA256(tenant_salt + raw_value)` — canonical, deterministic, tenant-isolated
- Encryption: Fernet for MVP; interface must be `EncryptionProvider` (pluggable for HSM, AWS KMS, Vault Transit in production)
- If encryption key is missing in INVESTIGATION mode: fail closed, never fall back to plaintext
- No raw PII in any log statement, exception message, stack trace, or debug output — ever

---

## Security & Governance Requirements

### Data residency

- PostgreSQL, MinIO, RabbitMQ, and Vault must be deployed within the designated residency region
- Encryption keys must never leave the residency region
- All audit logs must be stored locally — no external log shipping

### Audit trail requirements

Every extraction decision must be explainable:

```json
{
  "pii_id": "uuid",
  "pii_type": "ssn",
  "extraction_layer": "layer_1_pattern",
  "pattern_used": "\\d{3}-\\d{2}-\\d{4}",
  "confidence": 0.97,
  "page": 12,
  "bbox": [120, 340, 200, 355],
  "spans_pages": null,
  "context_snippet": "...employee [REDACTED] was hired...",
  "hashed_value": "sha256:...",
  "processed_at": "iso8601"
}
```

"The model decided" is never an acceptable audit entry. Every decision must trace to a specific rule, pattern, or classifier with logged inputs.

### LLM governance gate

LLM assist is off by default and requires explicit governance approval to enable:

```yaml
# config.yaml
llm_assist_enabled: false   # do not change without governance sign-off
llm_provider: "ollama"      # local only — never a cloud API
llm_model: "llama3"
llm_audit_log: true         # log every LLM call: prompt + response + decision
```

When enabled, LLM output is never used directly — it provides a candidate classification that is validated against Layer 1/2 patterns before being accepted.
