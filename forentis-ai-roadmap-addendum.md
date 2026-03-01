# Forentis AI — Roadmap Addendum

## Two Additions + Compliance Readiness Assessment

---

## Addition 1: Enhanced LLMCallLog with Data Locality Auditing

### Why
Law firms and breach response firms will ask one question before procurement: **"Does any breach data leave our network?"** The LLMCallLog already captures prompt/response for governance. Adding data locality verification gives customers **auditable proof** that no data left their environment.

### Changes to Existing Plan

**Modify `llm_call_logs` table** (Step 1 migration — or patch migration if Step 1 is done):

```
llm_call_logs (additions to existing schema)
  ...existing fields...
  data_locality       VARCHAR(16) NOT NULL DEFAULT 'local'   -- 'local' / 'remote'
  inference_endpoint   VARCHAR(512)                           -- actual Ollama URL used
  endpoint_verified    BOOLEAN DEFAULT false                  -- startup verification passed?
  network_isolated     BOOLEAN DEFAULT false                  -- no external egress confirmed?
```

**New: Startup verification check** in `app/llm/client.py`:

```python
class OllamaClient:
    def verify_locality(self) -> dict:
        """
        On startup, verify:
        1. Ollama endpoint resolves to localhost/private IP
        2. No proxy/tunnel detected
        3. Model weights are local (not streaming from remote)
        Returns verification dict logged to audit trail.
        """
```

**New: Data locality assertion in every LLM call:**

Every entry in `llm_call_logs` records:
- `data_locality: "local"` — confirmed local inference
- `inference_endpoint` — the actual URL (e.g., `http://localhost:11434`)
- `endpoint_verified: true` — startup check passed

**New API endpoint:**

```
GET /api/audit/llm-locality-report
```

Returns a summary suitable for auditors:
- Total LLM calls made
- All endpoints used
- Locality verification status
- Any failed checks or warnings
- Date range of the report

### Prompt to Execute (run as sub-agent after current work):

```
@agent-general-purpose Read CLAUDE.md for context.

1. Add data_locality (VARCHAR 16, default 'local'), inference_endpoint (VARCHAR 512),
   endpoint_verified (BOOLEAN), network_isolated (BOOLEAN) to LLMCallLog model in
   app/db/models.py.

2. Create a patch migration for these new columns.

3. In app/llm/client.py, add a verify_locality() method that checks:
   - Ollama URL resolves to 127.0.0.1 or private IP range (10.x, 172.16-31.x, 192.168.x)
   - Log warning if endpoint appears to be remote/proxied
   - Return a dict with verification results

4. Update app/llm/audit.py log_llm_call() to include data_locality,
   inference_endpoint, and endpoint_verified in every log entry.

5. Add GET /api/audit/llm-locality-report endpoint in app/api/routes/audit.py
   that returns a summary of all LLM calls, endpoints used, and verification status.

6. Add tests. Run pytest on changed files. Update CLAUDE.md.
```

---

## Addition 2: Expanded Data Categories (Phase 2)

### Current State
`by_category` in DensitySummary uses: PII, PHI, PFI

### Phase 2 Categories to Add

| Category | Abbreviation | Covers | Regulation | Priority |
|----------|:-:|---------|-----------|:--------:|
| Payment Card Information | PCI | Card numbers, CVV, expiration, cardholder name | PCI DSS | **High** |
| Sensitive PII | SPII | SSN, biometrics, driver's license, passport | NIST 800-122 | **High** |
| Non-Public Personal Information | NPI | Financial records, account balances, transactions | GLBA | **High** |
| Authentication Credentials | CREDENTIALS | Usernames + passwords, security questions, tokens, API keys | State breach laws | **High** |
| Federal Tax Information | FTI | Tax returns, taxpayer data, return information | IRS Pub 1075 | Medium |
| Biometric Data | BIOMETRIC | Fingerprints, facial recognition, retina, voiceprint | BIPA (IL), GDPR | Medium |
| Education Records | FERPA_DATA | Student records, grades, enrollment, disciplinary | FERPA | Low |
| Sensitive Personal Data (EU) | GDPR_SPECIAL | Race/ethnicity, political opinions, religion, sexual orientation, genetic | GDPR Art. 9 | Low |
| Children's Data | COPPA_DATA | Data from minors under 13 | COPPA | Low |

### Architecture: Multi-Category Mapping

A single entity type maps to **multiple** categories. This is critical because the same SSN triggers different regulations depending on context.

```python
# app/core/constants.py

ENTITY_CATEGORY_MAP = {
    # Identity
    "US_SSN":              ["PII", "SPII"],
    "US_PASSPORT":         ["PII", "SPII"],
    "US_DRIVER_LICENSE":   ["PII", "SPII"],
    "PERSON":              ["PII"],
    "EMAIL_ADDRESS":       ["PII"],
    "PHONE_NUMBER":        ["PII"],
    "DATE_OF_BIRTH":       ["PII"],
    "IP_ADDRESS":          ["PII"],
    "URL":                 ["PII"],

    # Financial
    "CREDIT_CARD":         ["PFI", "PCI"],
    "US_BANK_NUMBER":      ["PFI", "NPI"],
    "IBAN_CODE":           ["PFI", "NPI"],
    "SWIFT_CODE":          ["PFI"],
    "CRYPTO":              ["PFI"],

    # Health
    "MEDICAL_LICENSE":     ["PHI", "PII"],
    "NPI_NUMBER":          ["PHI"],

    # Tax
    "US_ITIN":             ["PII", "FTI"],
    "TAX_ID":              ["PII", "FTI"],

    # Credentials
    "PASSWORD":            ["CREDENTIALS"],
    "API_KEY":             ["CREDENTIALS"],
    "AWS_ACCESS_KEY":      ["CREDENTIALS"],

    # Biometric (custom entity types to add to Presidio)
    "FINGERPRINT_DATA":    ["PII", "SPII", "BIOMETRIC"],
    "FACIAL_RECOGNITION":  ["PII", "SPII", "BIOMETRIC"],
    "RETINA_SCAN":         ["PII", "SPII", "BIOMETRIC"],
    "VOICEPRINT":          ["PII", "SPII", "BIOMETRIC"],
}

DATA_CATEGORIES = {
    "PII":         {"label": "Personally Identifiable Information",  "regulation": "State breach laws, NIST 800-122"},
    "SPII":        {"label": "Sensitive PII",                        "regulation": "NIST 800-122, State laws"},
    "PHI":         {"label": "Protected Health Information",         "regulation": "HIPAA/HITECH"},
    "PFI":         {"label": "Personal Financial Information",       "regulation": "GLBA"},
    "PCI":         {"label": "Payment Card Information",             "regulation": "PCI DSS"},
    "NPI":         {"label": "Non-Public Personal Information",      "regulation": "GLBA"},
    "FTI":         {"label": "Federal Tax Information",              "regulation": "IRS Publication 1075"},
    "CREDENTIALS": {"label": "Authentication Credentials",          "regulation": "Various state laws"},
    "BIOMETRIC":   {"label": "Biometric Data",                      "regulation": "BIPA (Illinois), GDPR"},
    "FERPA_DATA":  {"label": "Education Records",                   "regulation": "FERPA"},
    "GDPR_SPECIAL":{"label": "GDPR Special Category Data",          "regulation": "GDPR Article 9"},
    "COPPA_DATA":  {"label": "Children's Data",                     "regulation": "COPPA"},
}
```

### Implementation Phases

**Phase 2a (do now — alongside current MVP):**
- PCI, SPII, NPI, CREDENTIALS — these cover 90% of US breach notifications
- Create the constants.py with full mapping
- Wire into density scoring

**Phase 2b (later):**
- FTI, BIOMETRIC — add custom Presidio recognizers for biometric patterns
- FERPA_DATA, GDPR_SPECIAL, COPPA_DATA — protocol-specific, enabled via ProtocolConfig

### Prompt to Execute:

```
@agent-general-purpose Read CLAUDE.md for context.

1. Create app/core/constants.py with ENTITY_CATEGORY_MAP and DATA_CATEGORIES
   as specified in docs/PLAN.md addendum. Map ALL entity types currently
   registered in our Presidio engine. Default unmapped types to ["PII"].

2. Update app/tasks/density.py: import ENTITY_CATEGORY_MAP. When computing
   by_category, use multi-category mapping so a single entity increments
   multiple category counters.

3. Update DensitySummary model docstring with expanded category example.

4. Add GET /api/categories endpoint that returns DATA_CATEGORIES metadata
   (useful for frontend dropdowns and report headers).

5. Create tests/test_constants.py:
   - Every Presidio entity type has a mapping
   - All mapped values are valid categories
   - Multi-category works (US_SSN → PII + SPII)

6. Run pytest on changed files. Update CLAUDE.md.
```

---

## Compliance Readiness Assessment

### Will this architecture clear auditor and legal firm checks?

**Short answer: Yes, with a few additions.** Your on-premise, air-gapped architecture is fundamentally strong. Here's what auditors and law firm IT/security teams typically look for, and where you stand:

### SOC 2 Trust Services Criteria

| Criteria | Requirement | Forentis AI Status | Gap? |
|----------|------------|-------------------|:----:|
| **Security** | Access controls, encryption, incident response | RBAC via review queues, audit trail exists | Add: authentication layer (JWT/SSO), encryption at rest |
| **Availability** | System uptime, backup/recovery | On-premise = customer responsibility | Add: deployment guide with backup recommendations |
| **Processing Integrity** | Accurate, complete processing | 1052 tests, deterministic pipeline, LLM audit log | ✅ Strong |
| **Confidentiality** | Data classification, retention | Multi-category classification, protocol-based | ✅ Strong |
| **Privacy** | Data handling, consent, retention | Breach data = no consent needed (legal basis) | Add: data retention/purge policy per project |

### HIPAA Technical Safeguards (for healthcare breaches)

| Safeguard | Requirement | Status | Gap? |
|-----------|------------|--------|:----:|
| Access Control | Unique user ID, emergency access, auto-logoff | Review queue RBAC exists | Add: user authentication, session timeout |
| Audit Controls | Record and examine access | AuditEvent table + LLMCallLog | ✅ Strong |
| Integrity Controls | Protect data from improper alteration | Deterministic pipeline, immutable audit trail | ✅ Strong |
| Transmission Security | Encrypt data in transit | On-premise = no transit | ✅ N/A (air-gapped) |
| Person Authentication | Verify identity of users | Not implemented | **Add: authentication** |

### Data Residency Checklist

| Check | Auditor Question | Your Answer |
|-------|-----------------|-------------|
| **Data location** | "Where is breach data stored?" | On customer's own server. SQLite/PostgreSQL local DB. No cloud. |
| **LLM processing** | "Does any data go to external AI APIs?" | No. Ollama runs locally. LLMCallLog proves it with endpoint verification. |
| **Model weights** | "Where are AI model files?" | Downloaded once via `ollama pull`, stored locally. No ongoing connection. |
| **Network egress** | "Does the application make outbound calls?" | No. Zero external dependencies at runtime. Fully air-gapped capable. |
| **Data retention** | "How long is breach data kept?" | Per-project configurable. Customer controls retention and deletion. |
| **Subprocessors** | "Who else touches the data?" | Nobody. No third-party services, no cloud APIs, no telemetry. |

### What You Need to Add for Full Compliance Readiness

**Priority 1 — Must have before enterprise sales:**

1. **Authentication & Authorization**
   - JWT-based auth or SSO/SAML integration
   - Role-based access: Admin, Investigator, QA Reviewer, Read-Only
   - Session management with timeout
   - This is the single biggest gap

2. **Encryption at Rest**
   - SQLite: use SQLCipher or move to PostgreSQL with TDE
   - File storage: encrypted volume or application-level encryption
   - Auditors will ask for this

3. **Data Retention & Purge**
   - Per-project data lifecycle: active → archived → purged
   - `DELETE /projects/{id}/purge` endpoint that removes all PII data
   - Audit log of what was purged and when (log survives purge)

**Priority 2 — Nice to have for compliance maturity:**

4. **Deployment Documentation**
   - Architecture diagram (single-page) showing data flow
   - Air-gapped deployment guide
   - Backup/restore procedures
   - This is what security teams review during vendor assessment

5. **Data Processing Agreement (DPA) Template**
   - Standard legal template your customers can sign
   - Covers: data handling, retention, breach notification, subprocessors (none)
   - A lawyer can draft this in a day

6. **Penetration Test Report**
   - Hire a firm to do a basic pentest of the API
   - Fixes any obvious issues (SQL injection, auth bypass, etc.)
   - Report becomes a sales asset

### Architecture Advantages for Compliance

Your architecture has several properties that make compliance **easier** than cloud alternatives:

| Property | Why It Helps |
|----------|-------------|
| **On-premise only** | No BAA needed with cloud providers. No GDPR cross-border issues. No subprocessor risk. |
| **Ollama local inference** | No AI data processing agreement needed. No model training on customer data. |
| **Deterministic primary pipeline** | LLM is additive, not required. Pipeline works without AI = simpler to audit. |
| **LLMCallLog with full audit** | Every AI decision is logged with prompt + response + acceptance. Auditors love this. |
| **Protocol-driven config** | Per-engagement rules stored in DB. Shows governance and configurability. |
| **Existing 1052 tests** | Demonstrates software quality. Testing coverage is an audit criterion. |
| **Data locality verification** | Proves no data left the network. Unique differentiator vs competitors. |

### Compliance Roadmap Summary

```
NOW (before first enterprise customer):
├── Authentication (JWT + RBAC)          ← biggest gap
├── LLMCallLog data locality fields      ← Addition 1 above
├── Expanded data categories             ← Addition 2 above
└── Architecture diagram (1-pager)

NEXT (within 3 months):
├── Encryption at rest
├── Data retention/purge per project
├── Air-gapped deployment guide
└── DPA template

LATER (6-12 months):
├── SOC 2 Type 1 audit
├── Penetration test
├── SSO/SAML integration
└── ISO 27001 consideration
```

---

*Document generated: March 2026*
*For: Forentis AI Development Team*
