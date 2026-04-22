"""LLM-powered record validation (Step 30h).

After extraction, validates records by asking the LLM to score each as
VALID or GARBAGE based on the document type context. No hardcoded rules —
the LLM understands what constitutes a real person vs a form code, legal
entity, or parsing artifact in any document type or locale.

Purged records cause their pages to appear as gaps, naturally triggering
the gap detection → self-correct → vision fallback chain.

Cost: 1 LLM call per document (~5 seconds).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import replace

logger = logging.getLogger(__name__)

# Max records per validation call to keep prompt manageable
_MAX_RECORDS_PER_CALL = 60


def validate_records(
    records: list,
    document_type: str,
    document_name: str,
    ollama_client,
    doc_id: str | None = None,
    expected_fields: list[str] | None = None,
    field_labels: list[str] | None = None,
) -> tuple[list, list, dict]:
    """Validate extracted records using LLM context awareness.

    Parameters
    ----------
    records:
        List of PIIRecord objects from extraction.
    document_type:
        From segregation (e.g., "Tax Document", "pension statement").
    document_name:
        File name for logging.
    ollama_client:
        OllamaClient instance.
    doc_id:
        Document UUID for audit logging.
    expected_fields:
        Canonical field types from segregation's ``field_inventory`` (e.g.
        ``["PERSON", "DATE_OF_BIRTH", "UK_NINO", "LOCATION"]``). Tells the
        validator what this specific document is *supposed* to contain,
        so partial-but-consistent records (name+NI+DOB without address
        on a pension doc) aren't purged as garbage. Without this, the
        validator falls back to generic heuristics.
    field_labels:
        Human-readable labels from segregation (e.g. ``["Member's Name",
        "Date of Birth", "National Insurance Number", "Last known
        address"]``). Helps the LLM judge which missing fields are
        tolerable absences vs red flags for this doc type.

    Returns
    -------
    tuple of (valid_records, purged_records, stats)
        valid_records: Records that passed validation.
        purged_records: Records flagged as garbage.
        stats: Dict with validation summary.
    """
    if not records or not ollama_client:
        return records, [], {"skipped": True}

    # Build the validation prompt with record summaries
    record_lines = []
    for i, r in enumerate(records[:_MAX_RECORDS_PER_CALL], 1):
        name = r.raw_name or ""
        gov_id = r.raw_government_id or ""
        phone = r.raw_phone or ""
        email = r.raw_email or ""
        page = r.page_range or "?"
        addr = ""
        if r.raw_address:
            if isinstance(r.raw_address, dict):
                addr = r.raw_address.get("raw", r.raw_address.get("full", ""))
            else:
                addr = str(r.raw_address)

        fields = []
        if name:
            fields.append(f'name="{name}"')
        else:
            fields.append('name=""')
        if gov_id:
            fields.append(f'gov_id="{gov_id}"')
        if phone:
            fields.append(f'phone="{phone}"')
        if addr:
            fields.append(f'address="{addr[:60]}"')
        if email:
            fields.append(f'email="{email}"')

        record_lines.append(f"{i}. Page {page}: {', '.join(fields)}")

    records_text = "\n".join(record_lines)

    # Build the field-contract section from segregation output. If the
    # caller didn't provide one, fall back to generic phrasing. The
    # contract tells the LLM what fields THIS document is supposed to
    # have — so a pension record missing address is 3-of-4 and still
    # valid, not "garbage"; a medical record missing gov_id but having
    # MRN is also valid; an HR record missing SSN but having employee_id
    # is valid. The validator adapts to the doc type instead of
    # hard-coding which fields are mandatory.
    if expected_fields or field_labels:
        contract_lines = ["Field contract (from document segregation):"]
        if expected_fields:
            contract_lines.append(f"  Expected field types: {', '.join(expected_fields)}")
        if field_labels:
            contract_lines.append(f"  Labels seen on the page: {', '.join(field_labels[:10])}")
        contract_section = "\n".join(contract_lines) + "\n\n"
        contract_rule = (
            "- A record with the MAJORITY of contract fields populated "
            "(e.g. name + at least one of: gov_id, DOB, address, email, phone) "
            "is VALID by definition. This person was identified by the "
            "extractor AND their data matches the document's contract — that's "
            "as solid as breach-notification evidence gets. Do NOT flag such "
            "records as garbage even if the name looks unusual, the address is "
            "in a different country, or the format differs from your training "
            "distribution.\n"
            "- PARTIAL records (name + one anchor only) are VALID. Breach "
            "notifications must reach any real person identified, even with "
            "incomplete data.\n"
            "- GARBAGE is for records that have a MISSING or MALFORMED name "
            "(e.g. 'FORFEITURE', 'TOTAL', just a page number), OR a name that "
            "is clearly an institution (trust, LLC, corporation, 'Pension "
            "Scheme'), OR data that doesn't correspond to a real individual "
            "person at all.\n"
        )
    else:
        contract_section = ""
        contract_rule = (
            "- When unsure, lean toward VALID (let the auditor decide).\n"
        )

    prompt = (
        f"You are validating extracted records from a {document_type} document "
        f"named '{document_name}'.\n\n"
        f"{contract_section}"
        f"For each record below, determine if it represents a REAL INDIVIDUAL PERSON "
        f"or if it's GARBAGE (parsing artifact, form code, legal entity, institutional "
        f"name, or other non-person data).\n\n"
        f"Common garbage patterns (but use your judgment, don't just match these):\n"
        f"- Empty or very short names\n"
        f"- Legal entities (trusts, LLCs, corporations, partnerships)\n"
        f"- Form/schedule identifiers mistaken for gov IDs\n"
        f"- Document reference numbers mistaken for phone numbers\n"
        f"- Institutional names (schools, hospitals, government agencies)\n\n"
        f"Records to validate:\n{records_text}\n\n"
        f"Return a JSON array with one object per record:\n"
        f'[{{"record": 1, "verdict": "VALID"}}, '
        f'{{"record": 2, "verdict": "GARBAGE", "reason": "brief reason"}}]\n\n'
        f"Rules:\n"
        f"- VALID = this is a real individual person whose data was breached\n"
        f"- GARBAGE = not a real person, or data is clearly wrong/garbled\n"
        f"{contract_rule}"
        f"- Return ONLY JSON array\n"
    )

    try:
        response = ollama_client.generate(
            prompt=prompt,
            system="You validate data extraction quality. Be precise and concise.",
            use_case="record_validation",
            document_id=doc_id,
        )
    except Exception as e:
        logger.warning(
            "Record validation LLM failed for %s: %s — keeping all records",
            document_name, e,
        )
        return records, [], {"error": str(e)}

    if not response:
        return records, [], {"error": "empty response"}

    # Parse LLM response
    verdicts = _parse_validation_response(response, len(records))
    if not verdicts:
        logger.warning(
            "Record validation: could not parse response for %s — keeping all records",
            document_name,
        )
        return records, [], {"parse_error": True}

    # Split records into valid and garbage.
    # Safety override: auto-reinstate when the LLM flags a record as
    # GARBAGE but the record has a real name + enough anchor fields
    # populated (relative to the document's contract). The LLM is
    # non-deterministic; between identical runs it has purged real
    # members that had complete data. This override is adaptive —
    # threshold scales with contract size so thin-schema docs (contact
    # list: PERSON + PHONE) don't get over-protected to zero and rich-
    # schema docs (pension: PERSON + NI + DOB + addr) still apply a
    # meaningful floor.
    valid = []
    purged = []
    reinstated = 0

    # Map contract field types to anchor categories this validator
    # recognises. Names are not anchors (they're the subject itself).
    _CONTRACT_ANCHOR_TYPES = {
        "US_SSN", "UK_NINO", "NI_NUMBER", "AADHAAR", "PAN", "IN_PAN",
        "NATIONAL_ID", "GOV_ID", "OTHER_ID", "TAX_ID", "GOVERNMENT_ID",
        "US_DRIVER_LICENSE", "DRIVER_LICENSE", "US_PASSPORT", "PASSPORT",
        "STUDENT_ID", "EMPLOYEE_ID", "EMPLOYER_ID", "MEDICAL_RECORD",
        "MRN", "INSURANCE_ID", "ACCOUNT_NUMBER", "MEMBER_ID",
        "DATE_OF_BIRTH",
        "LOCATION", "ADDRESS",
        "EMAIL_ADDRESS", "EMAIL",
        "PHONE_NUMBER", "PHONE_US", "PHONE_INTL",
        "CREDIT_CARD", "BANK_ACCOUNT", "US_BANK_NUMBER",
    }

    # Count how many anchor fields the contract expects for this doc.
    contract_anchor_count = 0
    if expected_fields:
        contract_anchor_count = sum(
            1 for f in expected_fields
            if f and f.upper() in _CONTRACT_ANCHOR_TYPES
        )
    # Threshold: at least half of contract's expected anchors, but
    # always ≥1 (so a single-anchor contract still protects real
    # records). When no contract is available, fall back to ≥2 for
    # conservative behaviour.
    if contract_anchor_count > 0:
        required_anchors = max(1, (contract_anchor_count + 1) // 2)
    else:
        required_anchors = 2

    def _is_real_name(n):
        if not n or len(n.strip()) < 3:
            return False
        # Obvious non-person tokens (form codes, totals, boilerplate)
        bad = {"forfeiture", "total", "subtotal", "summary", "grand total",
               "trust", "llc", "corp", "corporation", "inc", "ltd", "plc",
               "scheme", "fund", "partnership"}
        tokens = n.lower().split()
        if all(t in bad for t in tokens):
            return False
        return True

    def _anchor_count(r):
        n = 0
        if r.raw_government_id: n += 1
        if r.raw_dob: n += 1
        if r.raw_address: n += 1
        if r.raw_email: n += 1
        if r.raw_phone: n += 1
        return n

    for i, r in enumerate(records):
        idx = i + 1  # 1-indexed
        verdict = verdicts.get(idx, "VALID")  # default to VALID if not mentioned
        if (verdict == "GARBAGE"
                and _is_real_name(r.raw_name)
                and _anchor_count(r) >= required_anchors):
            verdict = "VALID"
            reinstated += 1
        if verdict == "GARBAGE":
            purged.append(r)
        else:
            valid.append(r)

    stats = {
        "total": len(records),
        "valid": len(valid),
        "purged": len(purged),
        "purge_rate": round(len(purged) / max(len(records), 1) * 100, 1),
    }

    logger.info(
        "Record validation for %s: %d/%d valid, %d purged (%.1f%% garbage)%s",
        document_name, len(valid), len(records), len(purged), stats["purge_rate"],
        f", {reinstated} LLM-purges reinstated by anchor-count safety net" if reinstated else "",
    )

    stats["reinstated"] = reinstated
    return valid, purged, stats


def _parse_validation_response(response: str, record_count: int) -> dict[int, str]:
    """Parse LLM validation response into {record_num: verdict} dict."""
    # Clean response
    cleaned = response.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines)

    # Try JSON parse
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find JSON array
        match = re.search(r'\[.*\]', cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                return {}
        else:
            return {}

    if not isinstance(data, list):
        data = [data]

    verdicts: dict[int, str] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        rec_num = entry.get("record")
        verdict = entry.get("verdict", "").upper()
        if isinstance(rec_num, int) and verdict in ("VALID", "GARBAGE"):
            verdicts[rec_num] = verdict

    return verdicts
