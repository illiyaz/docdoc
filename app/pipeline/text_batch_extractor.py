"""Text LLM Batch Extraction (Step 37).

Reliable extraction path for text PDFs. Sends batches of 5 pages to
qwen2.5:7b with entity_role-aware prompts. Each call extracts ALL
primary subjects from those pages.

Replaces the fragile coordinate → table → Presidio fallback chain
for text documents. Vision is only used for truly scanned/image docs.

Feature-flagged: USE_TEXT_LLM_BATCH=true in .env.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from uuid import uuid4

from app.rra.entity_resolver import PIIRecord

logger = logging.getLogger(__name__)

# Batch config — 3 pages per batch balances speed vs accuracy.
# 5 pages caused timeouts and cross-page confusion.
DEFAULT_PAGES_PER_BATCH = 3
MAX_CHARS_PER_PAGE = 3000


def extract_with_markers(
    page_texts: dict[int, str],
    ollama_client,
    doc_id: str,
    markers: dict,
    pages_per_batch: int = 10,
    records_per_page: int = 1,
    field_inventory: list[str] | None = None,
    country_hint: str | None = None,
    field_labels: list[str] | None = None,
) -> list[PIIRecord]:
    """Strategy A: Extract using marker-filtered snippets.

    Python filters each page to ~5 lines around context markers,
    then sends tiny snippets to LLM. Much faster and more accurate
    than sending full page text.

    Parameters
    ----------
    page_texts:
        Dict mapping 0-based page numbers to full page text.
    ollama_client:
        OllamaClient instance.
    doc_id:
        Document UUID.
    markers:
        Dict from detect_markers() with name_after_label, name_before_label.
    pages_per_batch:
        Pages per LLM call (can be higher than text batch — snippets are tiny).
    records_per_page:
        Expected number of person records per page (1 = Category A/D, >1 = B/C).
    """
    from app.pipeline.repeating_unit_detector import filter_page_by_markers

    name_after = markers.get("name_after_label", "")
    name_before = markers.get("name_before_label", "")

    if not name_after and not name_before:
        return []

    # Filter all pages to snippets. Pass segregation field labels so the
    # filter widens around DOB / NI / address rows that sit below the name
    # marker instead of capturing only 8 lines around the name.
    snippets: dict[int, str] = {}
    for pg, text in page_texts.items():
        snippet = filter_page_by_markers(
            text, name_after, name_before,
            additional_labels=field_labels,
        )
        if snippet:
            snippets[pg] = snippet

    if not snippets:
        logger.warning("Marker filter produced 0 snippets from %d pages", len(page_texts))
        return []

    logger.info(
        "Marker filter: %d/%d pages have snippets (avg %d chars vs %d full)",
        len(snippets), len(page_texts),
        sum(len(s) for s in snippets.values()) // max(len(snippets), 1),
        sum(len(t) for t in page_texts.values()) // max(len(page_texts), 1),
    )

    # Batch snippets (can do 10 per call since they're tiny)
    sorted_pages = sorted(snippets.keys())
    all_records: list[PIIRecord] = []
    calls_made = 0
    _marker_consec_fail = 0

    for batch_start in range(0, len(sorted_pages), pages_per_batch):
        if _marker_consec_fail >= 5:
            logger.error("Marker extraction: circuit breaker — 5 consecutive failures, aborting")
            break

        batch_pages = sorted_pages[batch_start:batch_start + pages_per_batch]

        batch_text = ""
        for pg in batch_pages:
            batch_text += f"\n--- PAGE {pg + 1} ---\n{snippets[pg]}\n"

        try:
            # Build field-aware prompt using segregation inventory
            _fi = field_inventory or []
            _extra_fields = ""
            _extra_json = ""
            _extra_rules = ""
            # Include OTHER_ID and NATIONAL_ID so non-US docs (UK NI number,
            # India PAN/Aadhaar, BR CPF, etc.) trigger the gov-ID branch.
            # Segregation emits OTHER_ID for IDs that don't cleanly map to
            # the US enum.
            if any(t in _fi for t in ("US_SSN", "GOV_ID", "IDENTIFICATION", "OTHER_ID", "NATIONAL_ID", "TAX_ID")):
                _extra_fields += ", SSN/Tax ID"
                _extra_json += ', "ssn": "123-45-6789"'
                _extra_rules += "- ssn: Social Security Number, Tax ID, National ID, or similar government identifier.\n"
            if any(t in _fi for t in ("DATE_OF_BIRTH",)):
                _extra_fields += ", date of birth"
                _extra_json += ', "dob": "01/15/1980"'
            if any(t in _fi for t in ("PHONE_NUMBER",)):
                _extra_fields += ", phone number"
                _extra_json += ', "phone": "555-123-4567"'
            if any(t in _fi for t in ("EMAIL_ADDRESS",)):
                _extra_fields += ", email"
                _extra_json += ', "email": "a@b.com"'
            if any(t in _fi for t in ("FINANCIAL", "US_BANK_NUMBER", "CREDIT_CARD")):
                _extra_fields += ", account numbers"
                _extra_json += ', "account": "1234567890"'

            if records_per_page > 1:
                _marker_prompt = (
                    f"Extract ALL persons' names, home addresses{_extra_fields} from each page snippet.\n"
                    f"Each page may contain {records_per_page} or more person records.\n"
                    f"Return JSON array with one object per PERSON (not per page):\n"
                    f'[{{"page": 1, "name": "Full Name", "address": "Street, City ST ZIP"{_extra_json}}}]\n'
                    f"Rules:\n"
                    f"- Extract ALL subjects, not just the first one\n"
                    f"- Address must be a real street address with a number\n"
                    f"{_extra_rules}"
                    f"- Use null for any field not found. Return ONLY JSON array.\n\n"
                    f"{batch_text}"
                )
            else:
                _marker_prompt = (
                    f"Extract the person's name, home address{_extra_fields} from each page snippet.\n"
                    f"Return JSON array:\n"
                    f'[{{"page": 1, "name": "Full Name", "address": "Street, City ST ZIP"{_extra_json}}}]\n'
                    f"Rules:\n"
                    f"- One person per page snippet\n"
                    f"- Address must be a real street address with a number\n"
                    f"{_extra_rules}"
                    f"- Use null for any field not found. Return ONLY JSON array.\n\n"
                    f"{batch_text}"
                )
            response = ollama_client.generate(
                prompt=_marker_prompt,
                system="You are a data extraction assistant. Return only JSON.",
                use_case="marker_extraction",
                document_id=doc_id,
            )
            calls_made += 1
            _marker_consec_fail = 0

            batch_page_texts = {pg: page_texts.get(pg, "") for pg in batch_pages}
            records = _parse_batch_response(response, doc_id, batch_pages, batch_page_texts, country_hint=country_hint)
            all_records.extend(records)

        except Exception:
            _marker_consec_fail += 1
            logger.warning(
                "Marker extraction failed for pages %s (consecutive=%d) — retrying individually",
                [p + 1 for p in batch_pages], _marker_consec_fail,
            )
            for retry_pg in batch_pages:
                if _marker_consec_fail >= 5:
                    break
                try:
                    retry_resp = ollama_client.generate(
                        prompt=(
                            f"Extract the person's name, address{_extra_fields} from this snippet.\n"
                            f'Return JSON: [{{"page": {retry_pg + 1}, "name": "Full Name", "address": "Street, City ST ZIP"{_extra_json}}}]\n\n'
                            f"--- PAGE {retry_pg + 1} ---\n{snippets[retry_pg]}\n"
                        ),
                        system="Return only JSON.",
                        use_case="marker_extraction_retry",
                        document_id=doc_id,
                    )
                    calls_made += 1
                    _marker_consec_fail = 0  # model alive
                    retry_page_texts = {retry_pg: page_texts.get(retry_pg, "")}
                    retry_records = _parse_batch_response(retry_resp, doc_id, [retry_pg], retry_page_texts, country_hint=country_hint)
                    all_records.extend(retry_records)
                except Exception:
                    _marker_consec_fail += 1
                    logger.debug("Marker retry failed for page %d (consecutive=%d)", retry_pg + 1, _marker_consec_fail)

    logger.info(
        "Marker extraction: %d records from %d snippets (%d LLM calls)",
        len(all_records), len(snippets), calls_made,
    )

    return all_records


def extract_text_batch(
    page_texts: dict[int, str],
    ollama_client,
    doc_id: str,
    document_type: str = "unknown",
    field_inventory: list[str] | None = None,
    pages_per_batch: int = DEFAULT_PAGES_PER_BATCH,
    record_unit: str = "page",
    records_per_page: int = 1,
    country_hint: str | None = None,
) -> list[PIIRecord]:
    """Extract PII from text pages using batched LLM calls.

    Parameters
    ----------
    page_texts:
        Dict mapping 0-based page numbers to extracted text.
    ollama_client:
        OllamaClient instance (uses default text model).
    doc_id:
        Document UUID for record attribution.
    document_type:
        Hint from analysis (e.g., "grade_report", "payroll", "medical").
    field_inventory:
        Expected field types from segregation (e.g., ["PERSON", "LOCATION"]).
    pages_per_batch:
        Number of pages per LLM call.
    record_unit:
        From repeating unit detection: "page" | "block" | "row" | "multi_page".
    records_per_page:
        Expected person records per page (1 for Category A/D, >1 for B/C).

    Returns
    -------
    list[PIIRecord]
        One record per primary subject found.
    """
    if not page_texts or not ollama_client:
        return []

    # Sort pages
    sorted_pages = sorted(page_texts.keys())

    # Filter out blank pages
    content_pages = [
        pg for pg in sorted_pages
        if len(page_texts[pg].strip()) > 5
    ]

    if not content_pages:
        return []

    all_records: list[PIIRecord] = []
    calls_made = 0
    consecutive_failures = 0       # circuit breaker — model health signal
    total_retries = 0
    failed_pages: list[int] = []   # pages that failed even after retry
    CIRCUIT_BREAKER_THRESHOLD = 5  # 5 consecutive failures = model is down

    # Overlapping windows when records can span page boundaries
    # Step size = batch size - overlap. Default overlap=0, but overlap=1
    # when multi-page records detected.
    use_overlap = record_unit == "multi_page"
    step_size = max(1, pages_per_batch - (1 if use_overlap else 0))

    # Build prompt components once (reused across batches)
    fields_hint = ", ".join(field_inventory) if field_inventory else "PERSON, LOCATION, PHONE_NUMBER, DATE_OF_BIRTH, US_SSN"
    _system_prompt = (
        "You are a document data extraction assistant. "
        "Extract ONLY the primary subject's information from each page. "
        "Ignore teachers, doctors, providers, institutional staff, and other supporting names."
    )

    # Post-batch quality gate state — after first batch, check if we're
    # extracting the fields segregation expected.  If not, ask the LLM
    # to diagnose what's wrong and adjust the prompt dynamically.
    _quality_gate_done = False
    _quality_adjusted_hint = fields_hint  # may be overridden by gate

    # Process in batches
    for batch_start in range(0, len(content_pages), step_size):
        # Circuit breaker — if model is consistently failing, stop early
        if consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
            remaining = content_pages[batch_start:]
            failed_pages.extend(remaining)
            logger.error(
                "Circuit breaker tripped: %d consecutive failures — "
                "aborting extraction for remaining %d pages. "
                "LLM model may be overloaded or down.",
                consecutive_failures, len(remaining),
            )
            break

        batch_pages = content_pages[batch_start:batch_start + pages_per_batch]

        # Build batch text
        batch_text = ""
        for pg in batch_pages:
            text = page_texts[pg][:MAX_CHARS_PER_PAGE]
            batch_text += f"\n--- PAGE {pg + 1} ---\n{text}\n"

        prompt = _build_batch_prompt(batch_pages, batch_text, document_type, fields_hint, record_unit, records_per_page)

        try:
            response = ollama_client.generate(
                prompt=prompt,
                system=_system_prompt,
                use_case="text_batch_extraction",
                document_id=doc_id,
            )
            calls_made += 1
            consecutive_failures = 0  # reset on success

            batch_page_texts = {pg: page_texts.get(pg, "") for pg in batch_pages}
            records = _parse_batch_response(response, doc_id, batch_pages, batch_page_texts, country_hint=country_hint)
            all_records.extend(records)

            # --- Post-batch quality gate (runs once after first successful batch) ---
            if not _quality_gate_done and records and field_inventory:
                _quality_gate_done = True
                _extracted_types = set()
                for r in records:
                    _extracted_types.update(r.entity_types_found)
                _expected = set(field_inventory)
                _missing = _expected - _extracted_types - {"PERSON"}
                if _missing and len(_missing) >= 2:
                    _sample_page = batch_pages[0]
                    _sample_text = page_texts.get(_sample_page, "")[:2000]
                    try:
                        _diag_prompt = (
                            f"I extracted these fields from a {document_type} document: {sorted(_extracted_types)}\n"
                            f"But I expected to also find: {sorted(_missing)}\n\n"
                            f"Here is the page text:\n{_sample_text}\n\n"
                            f"Are the missing fields ({', '.join(sorted(_missing))}) present on this page "
                            f"but in a different format or label? If yes, describe the format. "
                            f"If they are genuinely not on this page, say NOT_PRESENT.\n"
                            f"Reply in one short paragraph."
                        )
                        _diag_resp = ollama_client.generate(
                            prompt=_diag_prompt,
                            system="You analyze document structure. Be concise.",
                            use_case="quality_gate_diagnosis",
                            document_id=doc_id,
                        )
                        calls_made += 1
                        if _diag_resp and "NOT_PRESENT" not in _diag_resp.upper():
                            _quality_adjusted_hint = fields_hint + f"\n\nIMPORTANT: {_diag_resp.strip()}"
                            fields_hint = _quality_adjusted_hint
                            logger.info(
                                "Quality gate: missing %s — LLM adjusted hints: %s",
                                sorted(_missing), _diag_resp[:200],
                            )
                        else:
                            logger.info(
                                "Quality gate: %s genuinely not present on this doc type",
                                sorted(_missing),
                            )
                    except Exception:
                        logger.debug("Quality gate diagnosis failed", exc_info=True)

        except Exception:
            # Batch failed — retry each page individually.
            # Every page gets a fair retry; circuit breaker handles model-down.
            consecutive_failures += 1
            logger.info(
                "Batch failed for pages %s — retrying individually (consecutive=%d)",
                [p + 1 for p in batch_pages], consecutive_failures,
            )
            for retry_pg in batch_pages:
                if consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                    failed_pages.append(retry_pg)
                    continue
                retry_text = page_texts[retry_pg][:MAX_CHARS_PER_PAGE]
                if len(retry_text.strip()) < 5:
                    continue
                retry_prompt = _build_batch_prompt(
                    [retry_pg], f"\n--- PAGE {retry_pg + 1} ---\n{retry_text}\n",
                    document_type, fields_hint, record_unit, records_per_page,
                )
                try:
                    retry_response = ollama_client.generate(
                        prompt=retry_prompt,
                        system=_system_prompt,
                        use_case="text_batch_extraction_retry",
                        document_id=doc_id,
                    )
                    calls_made += 1
                    total_retries += 1
                    consecutive_failures = 0  # individual retry succeeded — model is alive
                    retry_page_texts = {retry_pg: page_texts.get(retry_pg, "")}
                    retry_records = _parse_batch_response(
                        retry_response, doc_id, [retry_pg], retry_page_texts,
                        country_hint=country_hint,
                    )
                    all_records.extend(retry_records)
                except Exception:
                    total_retries += 1
                    consecutive_failures += 1
                    failed_pages.append(retry_pg)
                    logger.debug("Retry failed for page %d (consecutive=%d)", retry_pg + 1, consecutive_failures)

    if failed_pages:
        logger.warning(
            "Text batch extraction: %d pages failed after retry — will appear as gaps: %s",
            len(failed_pages),
            [p + 1 for p in failed_pages[:20]],  # log first 20
        )

    logger.info(
        "Text batch extraction: %d records from %d pages (%d LLM calls)",
        len(all_records), len(content_pages), calls_made,
    )

    # Dedup records from overlapping page windows — same name+page = same record
    if use_overlap:
        all_records = _dedup_overlap_records(all_records)

    # Post-extraction: remove names that appear on too many pages
    # (teachers/staff appear across 5+ pages, students appear on 1-2)
    all_records = _filter_frequent_names(all_records, content_pages)

    return all_records


def _dedup_overlap_records(records: list[PIIRecord]) -> list[PIIRecord]:
    """Remove duplicate records from overlapping page windows.

    When batches overlap by 1 page, the same person on the overlap page
    gets extracted twice. Dedup by (name_lower, page_range) — keep the
    record with more fields populated.
    """
    seen: dict[tuple[str, str], PIIRecord] = {}
    for r in records:
        key = (r.raw_name.strip().lower() if r.raw_name else "", r.page_range or "")
        if key[0] and key in seen:
            # Keep the one with more non-empty fields
            existing = seen[key]
            new_fields = sum(1 for v in [r.raw_phone, r.raw_email, r.raw_dob, r.raw_government_id] if v)
            old_fields = sum(1 for v in [existing.raw_phone, existing.raw_email, existing.raw_dob, existing.raw_government_id] if v)
            if new_fields > old_fields:
                seen[key] = r
        else:
            seen[key] = r

    deduped = list(seen.values())
    if len(deduped) < len(records):
        logger.info(
            "Overlap dedup: %d → %d records (removed %d duplicates)",
            len(records), len(deduped), len(records) - len(deduped),
        )
    return deduped


def _filter_frequent_names(
    records: list[PIIRecord],
    content_pages: list[int],
) -> list[PIIRecord]:
    """Remove names that appear on too many distinct pages.

    A student appears on 1-2 pages. A teacher/provider appears on 5+.
    This catches institutional names that slipped through the LLM prompt
    and the per-page validation.

    Threshold: if a last name appears on >10% of content pages AND on
    more than 3 distinct pages, it's likely institutional.
    """
    from collections import Counter

    if len(content_pages) < 5:
        return records  # too few pages to detect frequency

    # Count how many distinct pages each last name appears on
    name_pages: dict[str, set[str]] = {}
    for r in records:
        if r.entity_role != "primary_subject":
            continue
        last = r.raw_name.split()[-1].lower() if r.raw_name else ""
        if len(last) < 3:
            continue
        if last not in name_pages:
            name_pages[last] = set()
        name_pages[last].add(r.page_range or "")

    # Find suspiciously frequent names
    threshold_pages = max(3, len(content_pages) * 0.10)
    frequent_names: set[str] = set()
    for last_name, pages in name_pages.items():
        if len(pages) > threshold_pages:
            frequent_names.add(last_name)
            logger.info(
                "Filtering frequent name '%s' (%d pages > %.0f threshold)",
                last_name, len(pages), threshold_pages,
            )

    if not frequent_names:
        return records

    filtered = []
    dropped = 0
    for r in records:
        last = r.raw_name.split()[-1].lower() if r.raw_name else ""
        if last in frequent_names and r.entity_role == "primary_subject":
            dropped += 1
            continue
        filtered.append(r)

    if dropped:
        logger.info("Frequency filter: dropped %d records with institutional names", dropped)

    return filtered


def _build_batch_prompt(
    pages: list[int],
    batch_text: str,
    document_type: str,
    fields_hint: str,
    record_unit: str = "page",
    records_per_page: int = 1,
) -> str:
    """Build the extraction prompt for a batch of pages."""
    multi_subject = records_per_page > 1 or record_unit in ("block", "row")

    # Multi-subject intro — for payroll registers, tabular docs, etc.
    if multi_subject:
        intro = (
            f"Extract personal information from these {len(pages)} pages of a {document_type} document.\n\n"
            f"IMPORTANT: Each page contains MULTIPLE person records "
            f"(approximately {records_per_page} per page).\n"
            f"Extract ALL persons on each page, not just the first one.\n\n"
        )
        count_rule = (
            f"- Each page has ~{records_per_page} subjects. Return one object PER PERSON, not per page.\n"
            f"- If a page has 5 people, return 5 objects all with the same page number.\n"
        )
    else:
        intro = (
            f"Extract personal information from these {len(pages)} pages of a {document_type} document.\n\n"
        )
        count_rule = (
            f"- One object per page. If a page has no primary subject, omit it.\n"
        )

    return (
        f"{intro}"
        f"For EACH page, extract ONLY the PRIMARY SUBJECT — the person this record is ABOUT:\n"
        f"- In a school report: the STUDENT (not parents, not teachers)\n"
        f"- In a medical record: the PATIENT (not doctors, not nurses)\n"
        f"- In a bank statement: the ACCOUNT HOLDER (not the bank staff)\n"
        f"- In an HR file: the EMPLOYEE (not the HR manager)\n"
        f"- In a legal filing: the CLAIMANT/DEFENDANT (not the attorney)\n\n"
        f"Also extract the primary subject's SECONDARY CONTACT if present "
        f"(parent, spouse, guardian, emergency contact, next of kin).\n\n"
        f"Fields to look for: {fields_hint}\n\n"
        f"Return a JSON array with one object per person:\n"
        f"[\n"
        f'  {{"page": 1, "name": "John Smith", '
        f'"parent_or_guardian": "Mary Smith", '
        f'"address": "123 Main St, Springfield, IL 62701", '
        f'"phone": "555-123-4567", '
        f'"dob": "01/15/2005", '
        f'"ssn": "123-45-6789", '
        f'"email": "john@example.com"}}\n'
        f"]\n\n"
        f"CRITICAL RULES:\n"
        f"- name: The primary subject's full name ONLY. Not a parent, teacher, or provider.\n"
        f"- address: MUST be a real STREET ADDRESS with a number + street name.\n"
        f"  Good: '5720 HILLPOINTE CIR', '123 Main St, City, ST 12345'\n"
        f"  BAD: 'Final Grades S1 2013', 'please contact the office' — these are NOT addresses.\n"
        f"  The address is the subject's HOME address, usually with a street number, street name,\n"
        f"  city, state, and ZIP code. If you cannot find a street address, set address to null.\n"
        f"- phone: Personal phone number ONLY. Institutional phone numbers that appear in page\n"
        f"  headers (same on EVERY page) are NOT personal — set to null.\n"
        f"- parent_or_guardian: Secondary contact (parent, spouse, guardian, emergency contact).\n"
        f"  This is a related person, NOT the primary subject themselves.\n"
        f"{count_rule}"
        f"- Use null for any field not found.\n\n"
        f"Respond ONLY with the JSON array.\n\n"
        f"{batch_text}"
    )


def _parse_batch_response(
    response: str,
    doc_id: str,
    batch_pages: list[int],
    page_texts: dict[int, str] | None = None,
    country_hint: str | None = None,
) -> list[PIIRecord]:
    """Parse LLM JSON response into PIIRecord objects.

    When *page_texts* is provided, validates extracted values exist
    on the claimed page — prevents cross-page contamination in batches.
    """
    records: list[PIIRecord] = []

    # Clean response
    cleaned = response.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find JSON array in the response
        match = re.search(r'\[.*\]', cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                logger.debug("Could not parse batch response as JSON")
                return []
        else:
            return []

    if not isinstance(data, list):
        data = [data]

    for entry in data:
        if not isinstance(entry, dict):
            continue

        name = entry.get("name", "")
        if not name or not isinstance(name, str) or len(name.strip()) < 2:
            continue

        name = name.strip()

        # Resolve page number (LLM returns 1-indexed, we use 0-indexed internally)
        page = entry.get("page")
        if isinstance(page, int):
            page_0 = page - 1  # convert to 0-indexed
            page_str = str(page)
        elif isinstance(page, str) and page.isdigit():
            page_0 = int(page) - 1
            page_str = page
        else:
            page_0 = batch_pages[0] if batch_pages else 0
            page_str = str(page_0 + 1)

        # Get the actual page text for validation
        actual_page_text = ""
        if page_texts:
            actual_page_text = page_texts.get(page_0, "").lower()

        # Validate name exists on the claimed page — check LAST name
        # (more unique than first name) against full page text.
        # This catches LLM hallucinations where it invents plausible names.
        name_parts = name.split()
        name_last = name_parts[-1].lower() if len(name_parts) > 1 else name_parts[0].lower()
        if actual_page_text and len(name_last) >= 3 and name_last not in actual_page_text:
            logger.debug(
                "Dropping hallucinated name: '%s' (last='%s') not on page %s",
                name[:30], name_last, page_str,
            )
            continue

        # P2: Cross-validation — if the LLM returned both name and
        # parent_or_guardian, verify they are DIFFERENT people.
        # If name == guardian, the LLM confused the two roles.
        guardian_name = entry.get("parent_or_guardian", "") or ""
        if guardian_name and isinstance(guardian_name, str):
            guardian_name = guardian_name.strip()
            # Check if "name" is actually the guardian (LLM put guardian in name field)
            name_lower = name.lower()
            guardian_lower = guardian_name.lower()
            if name_lower == guardian_lower:
                logger.debug(
                    "Dropping: name '%s' same as guardian on page %s",
                    name[:30], page_str,
                )
                continue
            # Check if name is a substring of guardian (e.g., "Cynthia Abad" from "Julio & Cynthia Abad")
            if len(name_lower) > 4 and name_lower in guardian_lower:
                logger.debug(
                    "Dropping: name '%s' is part of guardian '%s' on page %s",
                    name[:30], guardian_name[:30], page_str,
                )
                continue

        # Build address dict — validate it's a real street address AND on the right page
        raw_address = None
        addr = entry.get("address")
        if addr and isinstance(addr, str) and len(addr.strip()) > 5:
            addr_clean = addr.strip()
            # Must contain a number (street addresses start with numbers)
            has_number = any(c.isdigit() for c in addr_clean[:10])
            # Must not be instructional text (common in page body, not addresses)
            bad_patterns = ["please contact", "if you have any question",
                           "final grades", "semester 1", "semester 2",
                           "daily basis", "login information",
                           "check skyward", "counseling office"]
            is_garbage = any(p in addr_clean.lower() for p in bad_patterns)

            # Validate address text appears on the claimed page
            on_page = True
            if actual_page_text and has_number:
                # Check first significant word of address (street number)
                addr_words = addr_clean.split()
                if addr_words and addr_words[0].lower() not in actual_page_text:
                    on_page = False
                    logger.debug(
                        "Dropping address '%s' — not found on page %s",
                        addr_clean[:30], page_str,
                    )

            if has_number and not is_garbage and on_page:
                raw_address = {"raw": addr_clean}

        # Build entity_types
        entity_types = ["PERSON"]
        if raw_address:
            entity_types.append("LOCATION")

        phone = entry.get("phone")
        if phone and isinstance(phone, str) and len(phone) >= 7:
            entity_types.append("PHONE_NUMBER")
        else:
            phone = None

        dob = entry.get("dob")
        if dob and isinstance(dob, str) and len(dob) >= 4:
            entity_types.append("DATE_OF_BIRTH")
        else:
            dob = None

        ssn = entry.get("ssn")
        if ssn and isinstance(ssn, str) and len(ssn) >= 4:
            entity_types.append("US_SSN")
        else:
            ssn = None

        email = entry.get("email")
        if email and isinstance(email, str) and "@" in email:
            entity_types.append("EMAIL_ADDRESS")
        else:
            email = None

        rec = PIIRecord(
            record_id=str(uuid4()),
            entity_type="PERSON",
            normalized_value=name,
            raw_name=name,
            raw_address=raw_address,
            raw_phone=phone,
            raw_email=email,
            raw_dob=dob,
            raw_government_id=ssn,
            country=country_hint or "US",
            source_document_id=doc_id,
            page_range=page_str,
            entity_types_found=tuple(entity_types),
            entity_role="primary_subject",
        )
        records.append(rec)

        # Guardian as separate record — shares primary subject's address
        guardian = entry.get("parent_or_guardian")
        if guardian and isinstance(guardian, str) and len(guardian.strip()) >= 2:
            g_entity_types = ["PERSON"]
            if raw_address:
                g_entity_types.append("LOCATION")
            g_rec = PIIRecord(
                record_id=str(uuid4()),
                entity_type="PERSON",
                normalized_value=guardian.strip(),
                raw_name=guardian.strip(),
                raw_address=raw_address,  # same address as primary
                country=country_hint or "US",
                source_document_id=doc_id,
                page_range=page_str,
                entity_types_found=tuple(g_entity_types),
                entity_role="guardian",
            )
            records.append(g_rec)

    return records
