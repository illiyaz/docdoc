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

    # PDF-structure-derived expected count (BIG_FIXES #A2).
    # Old heuristic (pages_after_onset // 3) was a density guess. Now we
    # also scan the PDF for independent structural signals: repeating
    # section markers (from segregation) and unique gov-ID patterns per
    # page. If the structural estimate is higher, we use it — this
    # prevents the self-referential "we extracted 79% of our roster so
    # we're fine" trap where the roster itself came from the already-
    # purged extraction.
    heuristic_expected = max(3, pages_after_onset // 3)
    structural_expected = _estimate_subjects_from_pdf_structure(doc, seg, onset)
    expected = max(heuristic_expected, structural_expected)
    if structural_expected > heuristic_expected:
        logger.info(
            "Completeness: PDF structure suggests ~%d members (heuristic said %d) — using %d",
            structural_expected, heuristic_expected, expected,
        )

    # Speed optimization (task #26): skip the completeness vision trigger
    # for documents that are inherently single-subject.
    # Guards to prevent quality loss from mis-classified docs:
    #   - Escape hatch (DISABLE_TRIVIAL_SKIPS)
    #   - Segregation confidence >= TRIVIAL_SKIP_MIN_CONFIDENCE
    #   - Field inventory count consistent with single-subject (2-7 fields)
    #   - Already extracted at least 1 subject (so if initial extraction
    #     completely failed, vision recovery still runs as a safety net)
    #   - Exact doc_type string match against curated whitelist
    _single_subject_doc_types = {
        "identification_document", "insurance_card", "passport_data_page",
        "drivers_license", "ssn_card", "pay_stub", "money_order",
        "personal_check", "wire_transfer_confirmation", "receipt",
        "credit_card_statement", "bank_statement", "financial",
        "academic_transcript", "w2_form", "w4", "w4_filled",
        "1099_misc", "irs_notice",
    }
    _doc_type_lower = (doc_type or "").lower()
    _seg_conf = float(seg.get("confidence") or 0.0)
    _seg_fields_count = len(seg.get("fields", []) or [])
    if (
        not settings.disable_trivial_skips
        and total_pages <= 2
        and len(unique_names_only) >= 1
        and 2 <= _seg_fields_count <= 7
        and _seg_conf >= settings.trivial_skip_min_confidence
        and _doc_type_lower in _single_subject_doc_types
    ):
        logger.info(
            "Completeness: skipping vision trigger for single-subject doc %s "
            "(type=%s, pages=%d, names=%d, conf=%.2f, seg_fields=%d)",
            doc_name, doc_type, total_pages, len(unique_names_only),
            _seg_conf, _seg_fields_count,
        )
        # Tag for post-extraction anomaly sweep: flag the doc if final
        # subject count is suspiciously low so the auditor sees it.
        try:
            meta = dict(doc.metadata_json or {})
            marker = meta.get("_trivial_skip", {}) or {}
            marker["completeness_skipped"] = True
            marker["reason"] = "task_26_single_subject"
            meta["_trivial_skip"] = marker
            doc.metadata_json = meta
        except Exception:
            pass  # metadata tagging is best-effort
        return records

    completeness = found / max(expected, 1)
    logger.info(
        "Completeness check for %s: %d actionable subjects (of %d names), "
        "~%d expected (%.0f%% complete, %d pages after onset %d, expects_gov_id=%s)",
        doc_name, found, len(unique_names_only), expected,
        completeness * 100, pages_after_onset, onset, expects_gov_id,
    )

    # Threshold raised from 0.5 → 0.85 as part of BIG_FIXES #A2. At 50%
    # a document missing a quarter of its subjects would silently pass
    # (CMG: 26/33 = 79% passed the old gate but PDF had 34 real
    # members, 24% miss). 0.85 is aggressive but acceptable because:
    #   - structural_expected grounds it in PDF reality, not LLM-roster;
    #   - recovery is bounded by COMPLETENESS_VISION_MAX_PAGES (~15
    #     vision calls), so false triggers have a cost ceiling;
    #   - the alternative (missing real people) is a breach-notification
    #     failure, which is worse than some extra vision work.
    if completeness >= 0.85:
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

    # Step 2: Build a name→pages map (text-grep). Lets vision recovery
    # target pages that actually contain the missing people, instead of
    # sequentially scanning pages 0-28 and missing anyone in pages 30+.
    name_pages_map = _build_name_pages_map(doc, roster)

    # Step 3: Vision-extract targeted pages to find missing people.
    recovered = _vision_recover(
        doc=doc,
        missing_names=missing_names,
        name_pages_map=name_pages_map,
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


_ROSTER_PER_PAGE_BUDGET = 2000
_ROSTER_BATCH_CHAR_BUDGET = 20000


def _parse_roster_names(response: str) -> list[str]:
    """Parse an LLM roster response into a list of names.

    Accepts JSON array (preferred) or plain text with numbered/bulleted
    lines. Returns deduped, whitespace-trimmed names.
    """
    if not response:
        return []
    cleaned = response.strip()
    if cleaned.startswith("```"):
        cleaned = "\n".join(
            ln for ln in cleaned.split("\n")
            if not ln.strip().startswith("```")
        )

    names: list | None = None
    try:
        names = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r'\[.*\]', cleaned, re.DOTALL)
        if m:
            try:
                names = json.loads(m.group())
            except json.JSONDecodeError:
                names = None

    if names is None or not isinstance(names, list):
        names = []
        for line in cleaned.split("\n"):
            line = line.strip()
            line = re.sub(r'^[\d]+[.)]\s*', '', line)
            line = re.sub(r'^[-•*]\s*', '', line)
            line = line.strip().strip('"').strip("'")
            if (
                len(line) > 4
                and len(line.split()) >= 2
                and line[0].isupper()
                and not any(c in line for c in '{}[]():;/')
            ):
                names.append(line)

    valid = []
    for n in names:
        if isinstance(n, str) and len(n.strip()) > 2:
            valid.append(n.strip())
    return valid


def _get_name_roster(doc, ollama_client, doc_id: str, onset: int) -> list[str]:
    """Extract a roster of all individual names in a document.

    Roster v2 scans every page (batched to fit prompt budget) rather than
    stratified-sampling a subset. CMG pension benchmark showed the v1
    25-page sample caught only 14 of ~30+ members; the scatter of member
    names across all 100 pages meant half were on pages the sampler
    skipped. Batched full-doc scanning costs N extra LLM calls (≈5-9 for
    a 100-page doc) but yields a complete roster for the downstream
    vision-recovery step to target.
    """
    try:
        import fitz
        pdf = fitz.open(doc.source_path)
    except Exception:
        return []

    total_pages = pdf.page_count

    page_texts: list[tuple[int, str]] = []
    for pg in range(total_pages):
        text = pdf[pg].get_text() or ""
        pdf._forget_page(pg)
        if text.strip():
            page_texts.append((pg, text[:_ROSTER_PER_PAGE_BUDGET]))
    pdf.close()

    if not page_texts:
        return []

    # Group pages into batches that fit within the per-call char budget.
    batches: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    current_len = 0
    for pg, text in page_texts:
        chunk_len = len(text) + 40  # +40 for page header overhead
        if current and current_len + chunk_len > _ROSTER_BATCH_CHAR_BUDGET:
            batches.append(current)
            current = []
            current_len = 0
        current.append((pg, text))
        current_len += chunk_len
    if current:
        batches.append(current)

    doc_type = (doc.metadata_json or {}).get("segregation", {}).get(
        "document_type", "document"
    )

    logger.info(
        "Completeness roster: scanning all %d text-bearing pages of %s "
        "in %d batch(es)",
        len(page_texts), doc.file_name, len(batches),
    )

    all_names: set[str] = set()
    for i, batch in enumerate(batches):
        batch_text = "".join(
            f"\n--- PAGE {pg + 1} ---\n{text}\n" for pg, text in batch
        )
        prompt = (
            f"This is a {doc_type} ({doc.file_name}).\n\n"
            f"Below are pages from the document (batch {i + 1} of {len(batches)}). "
            f"List ALL INDIVIDUAL PERSON NAMES you can find across these pages "
            f"(not companies, trusts, LLCs, or institutions). The same person "
            f"may appear on multiple pages — include each unique person once.\n\n"
            f"{batch_text}\n\n"
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
            logger.warning(
                "Roster batch %d/%d LLM failed for %s: %s",
                i + 1, len(batches), doc.file_name, e,
            )
            continue

        for name in _parse_roster_names(response):
            all_names.add(name)

    roster = sorted(all_names)
    logger.info(
        "Completeness roster: %d unique names found for %s (across %d batches)",
        len(roster), doc.file_name, len(batches),
    )
    return roster


def _estimate_subjects_from_pdf_structure(doc, seg: dict, onset: int) -> int:
    """Count member-page signals directly from the PDF (BIG_FIXES #A2).

    Independent of the extraction output — this is the "ground truth
    estimator" that tells completeness_checker how many people the
    document probably contains.

    Signals used (in priority order):
      1. Repeating section marker from segregation (``name_after_label``
         / ``name_before_label``). If segregation says each member has
         a "SUMMARY OF DETAILS" header, count pages containing it.
      2. Unique gov-ID pattern count across the PDF. One NI / SSN =
         one person, regardless of extraction success.

    Returns 0 when nothing's signal-worthy — caller falls back to the
    page-density heuristic.
    """
    try:
        import fitz
        import re as _re
    except ImportError:
        return 0

    source_path = getattr(doc, "source_path", None)
    if not source_path:
        return 0

    # Signal 1: marker repetition
    markers = seg.get("markers") or {}
    marker_strs: list[str] = []
    for key in ("name_after_label", "name_before_label"):
        val = markers.get(key)
        if isinstance(val, str) and len(val) >= 6:
            marker_strs.append(val.lower().strip())

    # Signal 2: gov-ID pattern
    # Covers US_SSN last-4 style (XXXXXnnnn / XXX-XX-nnnn), UK_NINO
    # (2 letters + 6 digits + 1 letter), generic 9-digit pure numeric.
    id_pats = [
        _re.compile(r"\bX{3,}[- ]?X{0,2}[- ]?\d{4}\b"),     # US masked SSN
        _re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),              # US raw SSN
        _re.compile(r"\b[A-Z]{2}\d{6}[A-Z]\b"),             # UK NI number
    ]

    try:
        pdf = fitz.open(source_path)
    except Exception:
        return 0

    marker_pages = 0
    unique_ids: set[str] = set()
    try:
        for pg in range(pdf.page_count):
            if pg < onset:
                continue
            text = pdf[pg].get_text() or ""
            if not text:
                continue
            # Marker pages
            text_lower = text.lower()
            if any(m in text_lower for m in marker_strs):
                marker_pages += 1
            # Gov IDs
            for pat in id_pats:
                for m in pat.finditer(text):
                    unique_ids.add(m.group(0))
            pdf._forget_page(pg)
    finally:
        pdf.close()

    # Normalize US SSNs: 'XXXXX2682', 'XXX-XX-2682', '123-45-6789' all
    # map to last-4 or whole-digit key so variants don't inflate count.
    normalized_ids: set[str] = set()
    for raw in unique_ids:
        digits = _re.sub(r"\D", "", raw)
        if len(digits) >= 4:
            # Use last 4 for masked/real SSN convergence
            if "X" in raw.upper() or "x" in raw:
                normalized_ids.add(digits[-4:])
            else:
                normalized_ids.add(digits)
        elif _re.fullmatch(r"[A-Z]{2}\d{6}[A-Z]", raw):
            normalized_ids.add(raw)

    # Filter obvious fakes ('0000', all-same-digit)
    normalized_ids.discard("0000")
    normalized_ids = {x for x in normalized_ids if not (x.isdigit() and len(set(x)) == 1)}

    # Pick the strongest signal
    id_estimate = len(normalized_ids)
    marker_estimate = marker_pages
    estimate = max(id_estimate, marker_estimate)

    if estimate:
        logger.debug(
            "PDF structure estimate for %s: markers=%d ids=%d → %d",
            getattr(doc, "file_name", "?"), marker_estimate, id_estimate, estimate,
        )
    return estimate


def _name_variants(name: str) -> list[str]:
    """Return lowercased variants of *name* for substring matching.

    Pension schemes, payroll registers, and many legacy systems render
    names as "LASTNAME, FIRST INITIAL" while LLM rosters return them in
    "First Last" order. This function produces both directions plus a
    last-name-only variant (low precision but catches dense-layout docs
    where only the surname appears on a given page).
    """
    tokens = [t for t in re.split(r"\s+", name.strip()) if t]
    if not tokens:
        return []
    variants: list[str] = [" ".join(tokens).lower()]  # original order
    if len(tokens) >= 2:
        # "LAST, FIRST MIDDLE" pension-style
        variants.append(f"{tokens[-1]}, {' '.join(tokens[:-1])}".lower())
        # "LAST FIRST" (no comma)
        variants.append(f"{tokens[-1]} {' '.join(tokens[:-1])}".lower())
        # Last name alone (lowest precision, keep for sparse matches)
        variants.append(tokens[-1].lower())
    return variants


def _build_name_pages_map(doc, roster: list[str]) -> dict[str, list[int]]:
    """Return a dict mapping each roster name to the pages where it appears.

    Matches multiple name variants (direct order, last-comma-first,
    last-space-first, last-only) so pension/payroll docs that render
    "SMITH, JOHN" still match an LLM roster entry "John Smith".

    Pages where text extraction fails (form-fields, scanned images) won't
    surface here — those rely on the fallback sequential scan in
    `_vision_recover`.
    """
    try:
        import fitz
        pdf = fitz.open(doc.source_path)
    except Exception:
        return {}

    name_to_pages: dict[str, list[int]] = {name: [] for name in roster}
    # Build variant list per roster entry so we look up each page in the
    # widest possible way.
    variants_by_name: list[tuple[str, list[str]]] = [
        (name, _name_variants(name)) for name in roster
    ]

    try:
        for pg in range(pdf.page_count):
            text = pdf[pg].get_text() or ""
            pdf._forget_page(pg)
            if not text.strip():
                continue
            text_lower = text.lower()
            for name, variants in variants_by_name:
                if any(v and v in text_lower for v in variants):
                    name_to_pages[name].append(pg)
    finally:
        pdf.close()
    return name_to_pages


def _vision_recover(
    doc,
    missing_names: list[str],
    name_pages_map: dict[str, list[int]],
    onset: int,
    total_pages: int,
    ollama_client,
    vision_model: str,
    doc_id: str,
    scan_step: int = 2,
    scan_window: int = 40,
    scan_max_pages: int = 15,
) -> list:
    """Vision-extract pages to find gov IDs for the missing people.

    Uses ``name_pages_map`` to target pages that actually contain missing
    names (from PyMuPDF text search). When the map yields too few pages
    — e.g. form-PDFs where text extraction is sparse — falls back to a
    sequential scan controlled by ``scan_step`` / ``scan_window`` /
    ``scan_max_pages``.
    """
    from app.rra.entity_resolver import PIIRecord

    try:
        from app.pdf.renderer import render_page_to_image
        import fitz
    except ImportError:
        return []

    recovered: list[PIIRecord] = []

    # Targeted pages: stratified by missing-name so every name gets
    # at least one sample when the budget allows (task #22 — stratified
    # N-sample redesign). Earlier logic sorted the union by page number,
    # which let a single name hog the first N pages and starve the rest.
    targeted: set[int] = set()
    per_name: dict[str, list[int]] = {}
    for name in missing_names:
        pages_for_name = sorted({p for p in name_pages_map.get(name, []) if p >= onset})
        if pages_for_name:
            per_name[name] = pages_for_name
            targeted.update(pages_for_name)

    pages_to_try: list[int]
    if per_name:
        # Round-robin across missing names until we fill the budget.
        stratified: list[int] = []
        seen: set[int] = set()
        name_queues = {n: list(pgs) for n, pgs in per_name.items()}
        while len(stratified) < scan_max_pages and any(name_queues.values()):
            for name in list(name_queues.keys()):
                if len(stratified) >= scan_max_pages:
                    break
                queue = name_queues[name]
                while queue:
                    pg = queue.pop(0)
                    if pg not in seen:
                        stratified.append(pg)
                        seen.add(pg)
                        break
                if not queue:
                    del name_queues[name]
        pages_to_try = stratified
        logger.info(
            "Vision recovery: stratified %d pages across %d missing names "
            "(of %d candidate pages) in %s",
            len(pages_to_try), len(per_name), len(targeted), doc.file_name,
        )
    else:
        # Fallback — no text-layer hits. Use the legacy sequential scan.
        pages_to_try = list(
            range(onset, min(total_pages, onset + scan_window), scan_step)
        )[:scan_max_pages]
        logger.info(
            "Vision recovery: no text-layer matches, falling back to "
            "sequential scan of %d pages in %s",
            len(pages_to_try), doc.file_name,
        )

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
