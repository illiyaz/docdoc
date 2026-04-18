"""Completeness-driven vision trigger (Step 30h).

After record validation, checks whether the number of unique subjects
found is significantly lower than expected. If so, sends a diagnostic
LLM call to build a name roster from the document's summary pages,
then vision-extracts specific pages to find the missing people.

This avoids blind vision scanning — it's targeted at exactly the pages
and people that text extraction missed.

Trigger: unique_subjects < expected_subjects * 0.5

Cost: 1 diagnostic LLM call + N vision calls (only for missing people).
"""
from __future__ import annotations

import json
import logging
import re
from uuid import uuid4

logger = logging.getLogger(__name__)


def check_completeness_and_recover(
    records: list,
    doc,
    ollama_client,
    settings,
    db_session=None,
) -> list:
    """Check extraction completeness and recover via vision if needed.

    Parameters
    ----------
    records:
        Current PIIRecord list (post-validation).
    doc:
        Document ORM object with metadata_json, source_path, etc.
    ollama_client:
        OllamaClient instance.
    settings:
        App settings (for vision model, etc.).

    Returns
    -------
    list
        Original records + any recovered records from vision.
    """
    if not records or not ollama_client or not doc:
        return records

    doc_meta = dict(doc.metadata_json or {})
    seg = doc_meta.get("segregation", {})
    if not isinstance(seg, dict):
        return records

    doc_type = seg.get("document_type", "unknown")
    doc_name = doc.file_name or "unknown"
    doc_id = str(doc.id)

    # Calculate expected vs found
    # Key insight: count unique names WITH government IDs, not just names.
    # 19 names without SSNs is useless for breach notification — what matters
    # is how many people we can actually notify (name + SSN/gov ID).
    onset = doc.content_onset_page or doc.sample_onset_page or 0
    total_pages = doc_meta.get("page_count") or _get_page_count(doc.source_path)
    if total_pages <= 0:
        return records

    pages_after_onset = max(1, total_pages - onset)

    # Count subjects with actionable PII (name + gov ID)
    unique_actionable = set()
    unique_names_only = set()
    for r in records:
        name = (r.raw_name or "").strip().upper()
        if name and len(name) > 2:
            unique_names_only.add(name)
            if r.raw_government_id and len(str(r.raw_government_id)) >= 5:
                unique_actionable.add(name)

    # Also check field inventory — if segregation expects US_SSN but we
    # have few records with gov IDs, that's a completeness failure
    seg_fields = seg.get("field_inventory", [])
    expects_gov_id = any(
        f in seg_fields
        for f in ("US_SSN", "GOV_ID", "IDENTIFICATION", "OTHER_ID")
    )

    # Use actionable count (name + gov ID) when doc is expected to have gov IDs
    if expects_gov_id:
        found = len(unique_actionable)
    else:
        found = len(unique_names_only)

    # Estimate expected subjects: at least 1 per 3 pages after onset
    expected = max(3, pages_after_onset // 3)

    completeness = found / max(expected, 1)
    logger.info(
        "Completeness check for %s: %d actionable subjects (of %d names), "
        "~%d expected (%.0f%% complete, %d pages after onset %d, expects_gov_id=%s)",
        doc_name, found, len(unique_names_only), expected,
        completeness * 100, pages_after_onset, onset, expects_gov_id,
    )

    if completeness >= 0.5:
        return records  # Good enough — don't trigger vision

    # --- Completeness is low — try to recover via vision ---
    logger.info(
        "Completeness trigger: %s has %d/%d subjects (%.0f%%) — "
        "attempting vision recovery",
        doc_name, found, expected, completeness * 100,
    )

    # Step 1: Get name roster from early pages (summary/TOC pages)
    roster = _get_name_roster(doc, ollama_client, doc_id, onset)
    if not roster:
        logger.info("Completeness: no roster found for %s", doc_name)
        return records

    # Filter roster to names that DON'T have actionable PII yet.
    # We have their names but not their SSNs — vision needs to find the SSNs.
    missing_names = [
        name for name in roster
        if name.upper() not in unique_actionable
    ]
    if not missing_names:
        logger.info(
            "Completeness: all %d roster names already have gov IDs for %s",
            len(roster), doc_name,
        )
        return records

    logger.info(
        "Completeness: %d names in roster, %d without gov IDs — recovering via vision "
        "(actionable: %s, missing: %s)",
        len(roster), len(missing_names),
        sorted(unique_actionable)[:3],
        missing_names[:5],
    )

    # Step 2: Vision-extract pages after onset to find missing people
    recovered = _vision_recover(
        doc=doc,
        missing_names=missing_names,
        onset=onset,
        total_pages=total_pages,
        ollama_client=ollama_client,
        vision_model=settings.ollama_vision_model,
        doc_id=doc_id,
        scan_step=max(1, settings.completeness_vision_step),
        scan_window=max(1, settings.completeness_vision_window),
        scan_max_pages=max(1, settings.completeness_vision_max_pages),
    )

    if recovered:
        logger.info(
            "Completeness recovery: found %d additional records for %s",
            len(recovered), doc_name,
        )
        records = list(records) + recovered

    return records


def _get_page_count(source_path: str) -> int:
    """Get PDF page count."""
    try:
        import fitz
        doc = fitz.open(source_path)
        count = doc.page_count
        doc.close()
        return count
    except Exception:
        return 0


def _get_name_roster(doc, ollama_client, doc_id: str, onset: int) -> list[str]:
    """Extract a roster of names from a stratified sample of the document.

    Summary/index pages alone miss members listed throughout the body (e.g.
    pension plans, employee registers, tax K-1 packets). This samples:
      (1) the first 10 pages (cover/TOC/summary),
      (2) 5 pages immediately before onset if onset is late,
      (3) ~15 stratified pages across the body for long documents.
    """
    try:
        import fitz
        pdf = fitz.open(doc.source_path)
    except Exception:
        return []

    total_pages = pdf.page_count
    sample_pages: list[int] = []

    for pg in range(min(10, total_pages)):
        sample_pages.append(pg)

    if onset > 10:
        for pg in range(max(0, onset - 5), onset):
            if pg not in sample_pages:
                sample_pages.append(pg)

    # Stratified sample across the body so names scattered throughout the
    # document make it into the roster, not just those on summary pages.
    if total_pages > 20:
        body_start = onset if onset else 10
        if total_pages > body_start:
            n_body_samples = 15
            step = max(1, (total_pages - body_start) // n_body_samples)
            added = 0
            for pg in range(body_start, total_pages, step):
                if pg in sample_pages:
                    continue
                sample_pages.append(pg)
                added += 1
                if added >= n_body_samples:
                    break

    sample_pages = sorted(set(sample_pages))

    per_page_budget = 1500 if len(sample_pages) > 15 else 2000
    total_budget = 25000
    page_text = ""
    for pg in sample_pages:
        if len(page_text) >= total_budget:
            break
        text = pdf[pg].get_text()
        pdf._forget_page(pg)
        if text.strip():
            page_text += f"\n--- PAGE {pg + 1} ---\n{text[:per_page_budget]}\n"

    pdf.close()

    if not page_text or len(page_text.strip()) < 50:
        return []

    logger.info(
        "Completeness roster: sampling %d pages (of %d) for %s",
        len(sample_pages), total_pages, doc.file_name,
    )

    # Ask LLM to find all individual names
    prompt = (
        f"This is a {doc.metadata_json.get('segregation', {}).get('document_type', 'document')} "
        f"({doc.file_name}).\n\n"
        f"Below is a stratified sample of pages from the document. List ALL "
        f"INDIVIDUAL PERSON NAMES you can find across these pages "
        f"(not companies, trusts, LLCs, or institutions). The same person "
        f"may appear on multiple pages — include each unique person once.\n\n"
        f"{page_text}\n\n"
        f"Return a JSON array of names:\n"
        f'["John Smith", "Jane Doe", ...]\n\n'
        f"If no individual names are found, return: []\n"
        f"Return ONLY the JSON array."
    )

    try:
        response = ollama_client.generate(
            prompt=prompt,
            system="Extract individual person names only. Return JSON array.",
            use_case="completeness_roster",
            document_id=doc_id,
        )
    except Exception as e:
        logger.warning("Completeness roster LLM failed: %s", e)
        return []

    if not response:
        return []

    # Parse response
    cleaned = response.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines)

    try:
        names = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r'\[.*\]', cleaned, re.DOTALL)
        if match:
            try:
                names = json.loads(match.group())
            except json.JSONDecodeError:
                names = None
        else:
            names = None

    # Fallback: if LLM returned plain text instead of JSON,
    # extract names from numbered/bulleted lines
    if names is None or not isinstance(names, list):
        names = []
        for line in cleaned.split("\n"):
            line = line.strip()
            # Strip numbering: "1. Name" or "- Name" or "• Name"
            line = re.sub(r'^[\d]+[.)]\s*', '', line)
            line = re.sub(r'^[-•*]\s*', '', line)
            line = line.strip().strip('"').strip("'")
            # Looks like a name: 2+ words, starts with capital, no special chars
            if (
                len(line) > 4
                and len(line.split()) >= 2
                and line[0].isupper()
                and not any(c in line for c in '{}[]():;/')
            ):
                names.append(line)
        if names:
            logger.info("Roster: parsed %d names from plain text (JSON failed)", len(names))

    # Filter to valid names
    valid = [
        n.strip() for n in names
        if isinstance(n, str) and len(n.strip()) > 2
    ]
    logger.info("Completeness roster: %d names found for %s", len(valid), doc.file_name)
    return valid


def _vision_recover(
    doc,
    missing_names: list[str],
    onset: int,
    total_pages: int,
    ollama_client,
    vision_model: str,
    doc_id: str,
    scan_step: int = 2,
    scan_window: int = 40,
    scan_max_pages: int = 15,
) -> list:
    """Vision-extract pages after onset to find missing people.

    scan_step=1 scans every page (GPU-class hardware). scan_step=2 samples every
    other page (M4/CPU — avoids thermal throttle). scan_window bounds how far
    past onset to look; scan_max_pages caps the LLM call budget.
    """
    from app.rra.entity_resolver import PIIRecord

    try:
        from app.pdf.renderer import render_page_to_image
        import fitz
    except ImportError:
        return []

    recovered: list[PIIRecord] = []

    pages_to_try = list(
        range(onset, min(total_pages, onset + scan_window), scan_step)
    )
    pages_to_try = pages_to_try[:scan_max_pages]

    if not pages_to_try:
        return []

    names_str = ", ".join(missing_names[:20])
    logger.info(
        "Vision recovery: scanning %d pages for %d missing people in %s",
        len(pages_to_try), len(missing_names), doc.file_name,
    )

    _doc_type = doc.metadata_json.get("segregation", {}).get("document_type", "document")

    for page_idx in pages_to_try:
        try:
            image_b64 = render_page_to_image(doc.source_path, page_idx, dpi=300)

            prompt = (
                f"This is page {page_idx + 1} of a {_doc_type} ({doc.file_name}).\n\n"
                f"Find any person's unique government-issued identification number "
                f"and mailing address on this page. The ID could be any national "
                f"identifier used in any country.\n\n"
                f"People I'm looking for: {names_str}\n\n"
                f"Read every number and code on the page carefully. The ID number "
                f"may be in a small box, field, or label. It is critical to "
                f"capture the exact number.\n\n"
                f"Return JSON array:\n"
                f'[{{"page": {page_idx + 1}, "name": "Full Name", '
                f'"gov_id": "the exact ID number", '
                f'"address": "full mailing address"}}]\n\n'
                f"If no person or no ID number is found, return: []\n"
                f"Return ONLY JSON."
            )

            response = ollama_client.generate_with_images(
                prompt=prompt,
                images=[image_b64],
                use_case="completeness_vision_recover",
                document_id=doc_id,
                model_override=vision_model,
            )

            if not response:
                continue

            # Parse
            cleaned = response.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                cleaned = "\n".join(lines)

            try:
                entries = json.loads(cleaned)
            except json.JSONDecodeError:
                match = re.search(r'\[.*\]', cleaned, re.DOTALL)
                if match:
                    try:
                        entries = json.loads(match.group())
                    except json.JSONDecodeError:
                        continue
                else:
                    continue

            if not isinstance(entries, list):
                entries = [entries]

            for entry in entries:
                name = entry.get("name", "")
                if not name or len(name.strip()) < 3:
                    continue

                gov_id = (
                    entry.get("gov_id")
                    or entry.get("ssn")
                    or entry.get("tax_id")
                    or entry.get("tin")
                    or entry.get("national_id")
                    or entry.get("ni_number")
                    or entry.get("pan")
                    or None
                )
                address = entry.get("address") or None
                dob = entry.get("dob") or entry.get("date_of_birth") or None

                entity_types = ["PERSON"]
                if address:
                    entity_types.append("LOCATION")
                if gov_id and len(str(gov_id)) >= 4:
                    entity_types.append("US_SSN")  # generic gov ID mapped to US_SSN for pipeline compat
                if dob:
                    entity_types.append("DATE_OF_BIRTH")

                _country_hint = (doc.metadata_json or {}).get("segregation", {}).get("country_hint") or "US"
                rec = PIIRecord(
                    record_id=str(uuid4()),
                    entity_type="PERSON",
                    normalized_value=name.strip(),
                    raw_name=name.strip(),
                    raw_address={"raw": address} if address else None,
                    raw_government_id=str(gov_id) if gov_id else None,
                    raw_dob=str(dob) if dob else None,
                    country=_country_hint,
                    source_document_id=doc_id,
                    page_range=str(page_idx + 1),
                    entity_types_found=tuple(entity_types),
                    entity_role="primary_subject",
                )
                recovered.append(rec)
                logger.info(
                    "Vision recovered: %s (SSN=%s) on page %d",
                    name.strip(), "yes" if gov_id else "no", page_idx + 1,
                )

        except Exception:
            logger.debug("Vision recovery failed for page %d", page_idx + 1, exc_info=True)

    return recovered
