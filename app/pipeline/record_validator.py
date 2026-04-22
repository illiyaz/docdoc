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
            "- PARTIAL but CONSISTENT records are VALID. A record missing one "
            "or two contract fields is not garbage — breach notifications must "
            "reach any real person identified, even with incomplete data. Only "
            "flag GARBAGE when the record is clearly not a real person (legal "
            "entity, form code, boilerplate, obvious parsing artifact).\n"
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

    # Split records into valid and garbage
    valid = []
    purged = []
    for i, r in enumerate(records):
        idx = i + 1  # 1-indexed
        verdict = verdicts.get(idx, "VALID")  # default to VALID if not mentioned
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
        "Record validation for %s: %d/%d valid, %d purged (%.1f%% garbage)",
        document_name, len(valid), len(records), len(purged), stats["purge_rate"],
    )

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
