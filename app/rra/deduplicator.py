"""Record deduplicator — Phase 2.

Takes ``ResolvedGroup`` objects from the entity resolver, builds a
``NotificationSubject`` ORM record for each group, and upserts them into
the database.  Duplicate subjects (same ``canonical_email`` or
``canonical_phone``) are merged rather than duplicated.

Canonical field selection strategy (``_best_value``):
  - Most frequent non-None value wins.
  - Tie on frequency → longest string wins.
  - Tie on length → alphabetically first wins.
"""
from __future__ import annotations

import logging
from collections import Counter
from uuid import uuid4

from sqlalchemy.orm import Session

from app.db.models import Document, NotificationSubject
from app.normalization.dob_normalizer import normalize_dob
from app.normalization.name_normalizer import normalize_name
from app.pipeline.record_mapper import _GOV_ID_FIELD_TYPES
from app.rra.entity_resolver import PIIRecord, ResolvedGroup

logger = logging.getLogger(__name__)

# Minimum PII threshold: a notification subject must have at least a name
# PLUS one of these corroborating PII types.  Name-only records are noise
# and should not generate notification subjects.
_CORROBORATING_PII_TYPES = frozenset({
    "US_SSN", "CREDIT_CARD", "BANK_ACCOUNT", "US_BANK_NUMBER",
    "US_BANK_ROUTING",
    "US_DRIVER_LICENSE", "US_PASSPORT", "NI_NUMBER", "AADHAAR",
    "PAN_CARD", "TAX_ID", "NATIONAL_INSURANCE_UK", "UK_NINO",
    "NATIONAL_ID", "DATE_OF_BIRTH", "DATE_OF_BIRTH_MDY",
    "DATE_OF_BIRTH_DMY", "EMAIL_ADDRESS", "EMAIL",
    "PHONE_NUMBER", "PHONE_US", "PHONE_INTL",
    "LOCATION", "ADDRESS", "PHI_MRN", "PHI_NPI",
    "MEDICAL_LICENSE", "BIOMETRIC",
    # Account/membership IDs — common in CSV/XLSX payroll and HR data.
    # These are meaningful identifiers that corroborate a person exists.
    "ACCOUNT_NUMBER", "MEMBER_ID", "EMPLOYEE_ID",
    "IBAN_CODE", "NPI_NUMBER",
})

# Entity types that are NOT meaningful on their own — they need either a name
# or at least one PII type from _CORROBORATING_PII_TYPES to form a valid subject.
# URL, US_BANK_NUMBER (matches any 8-17 digit string), and IP_ADDRESS are too
# noisy to justify a notification subject by themselves.
_LOW_VALUE_ENTITY_TYPES = frozenset({
    "URL", "US_BANK_NUMBER", "IP_ADDRESS", "LOCATION",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _best_value(values: list[str | None]) -> str | None:
    """Pick the canonical value from a list using frequency → length → alpha."""
    cleaned = [v for v in values if v]
    if not cleaned:
        return None

    counts = Counter(cleaned)
    max_count = max(counts.values())
    candidates = [v for v, c in counts.items() if c == max_count]

    # Tie-break: longest, then alphabetically first
    candidates.sort(key=lambda v: (-len(v), v))
    return candidates[0]


_NULL_ADDRESS_TOKENS = frozenset({
    "null", "none", "nan", "n/a", "na", "nil", "-", "--", "undefined",
    "(none)", "(null)", "not available", "not provided",
})


def _clean_address_token(value: str | None) -> str | None:
    """Return None if *value* is a null-ish placeholder; else stripped value."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.lower() in _NULL_ADDRESS_TOKENS:
        return None
    return s


def _clean_raw_address_string(raw: str) -> str:
    """Strip 'null null null' / standalone null tokens from address string.

    Handles the CMG-style dict rendering where missing street/city/state are
    stringified as "null". Collapses repeated whitespace and trailing commas.
    """
    import re as _re
    # Remove standalone null-ish tokens (word-bounded, case-insensitive)
    cleaned = _re.sub(
        r"\b(?:null|none|nan|n/a|na|nil|undefined)\b",
        "",
        raw,
        flags=_re.IGNORECASE,
    )
    # Collapse ", ," and runs of commas/whitespace
    cleaned = _re.sub(r"(?:\s*,\s*){2,}", ", ", cleaned)
    cleaned = _re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.strip(",;- ").strip()
    return cleaned


def _address_is_junk(addr: dict) -> bool:
    """Return True when an address dict has no meaningful content.

    Junk = no street/city/state/zip/raw after null-token cleanup.
    """
    if not addr:
        return True
    street = _clean_address_token(addr.get("street"))
    city = _clean_address_token(addr.get("city"))
    state = _clean_address_token(addr.get("state"))
    zp = _clean_address_token(addr.get("zip"))
    raw = _clean_address_token(addr.get("raw"))
    if raw:
        raw = _clean_raw_address_string(raw) or None
    # Need at least zip OR (street AND city) OR a non-trivial raw string
    if zp:
        return False
    if street and city:
        return False
    if raw and len(raw) >= 8 and any(ch.isdigit() for ch in raw):
        return False
    return True


def _sanitize_address(addr: dict | None) -> dict | None:
    """Null out placeholder tokens, strip 'null' from raw; drop if junk."""
    if not addr or not isinstance(addr, dict):
        return None
    cleaned = dict(addr)
    for key in ("street", "city", "state", "zip", "country"):
        if key in cleaned:
            cleaned[key] = _clean_address_token(cleaned.get(key))
    raw = cleaned.get("raw")
    if isinstance(raw, str):
        stripped = _clean_raw_address_string(raw)
        cleaned["raw"] = stripped or None
    if _address_is_junk(cleaned):
        return None
    return cleaned


def _best_address(addresses: list[dict | None]) -> dict | None:
    """Pick canonical address: most frequent postal code wins.

    Junk addresses (all-null tokens, empty dicts) are filtered upfront and
    null-token artifacts ('null null null') are stripped from raw strings.
    """
    valid = [_sanitize_address(a) for a in addresses]
    valid = [a for a in valid if a]
    if not valid:
        return None

    zips = [a.get("zip") or "" for a in valid]
    zip_counts = Counter(z for z in zips if z)
    if not zip_counts:
        return valid[0]

    best_zip = max(zip_counts, key=lambda z: (zip_counts[z], z))
    for a in valid:
        if (a.get("zip") or "") == best_zip:
            return a
    return valid[0]


# ---------------------------------------------------------------------------
# Deduplicator
# ---------------------------------------------------------------------------

class Deduplicator:
    """Build ``NotificationSubject`` rows from resolved groups and persist."""

    def __init__(self, db_session: Session) -> None:
        self.db = db_session

    def build_subjects(
        self,
        groups: list[ResolvedGroup],
    ) -> list[NotificationSubject]:
        """Convert *groups* into ``NotificationSubject`` rows and upsert.

        Returns the list of persisted (or merged) subjects.  The session
        is flushed but **not** committed — the caller owns the transaction.
        """
        subjects: list[NotificationSubject] = []
        skipped_thin = 0

        for group in groups:
            # Minimum-PII threshold: skip groups that have only a name and
            # no corroborating PII (SSN, DOB, email, phone, address, etc.).
            # These are noise — headers, labels, or orphan PERSON detections.
            has_corroboration = False
            has_name = False
            all_entity_types: set[str] = set()
            for r in group.records:
                if r.raw_name:
                    has_name = True
                if r.raw_email or r.raw_phone or r.raw_dob or r.raw_address or r.raw_government_id:
                    has_corroboration = True
                # Collect all entity types for low-value check
                if r.entity_types_found:
                    all_entity_types.update(et.upper() for et in r.entity_types_found)
                    for et in r.entity_types_found:
                        if et.upper() in _CORROBORATING_PII_TYPES:
                            has_corroboration = True

            if not has_corroboration:
                # Relaxation: if the group has 2+ records, the entity resolver
                # already decided they belong together.  If the group has a name
                # and ANY non-trivial entity type, keep it — the pipeline
                # intentionally merged them.  This prevents losing subjects from
                # EML/MSG docs where name + email were on the same page.
                if has_name and len(group.records) >= 2:
                    non_person_types = all_entity_types - {"PERSON", "PERSON_NAME"}
                    if non_person_types:
                        has_corroboration = True  # entity resolver merged them
                if not has_corroboration:
                    skipped_thin += 1
                    continue

            # Second filter: if no name and ALL entity types are low-value,
            # skip — these are noise (URL-only, IP-only, bank-number-only).
            if not has_name:
                meaningful_types = all_entity_types - _LOW_VALUE_ENTITY_TYPES - {"PERSON"}
                if not meaningful_types:
                    skipped_thin += 1
                    continue

            ns = self._build_one(group)
            existing = self._find_existing(ns)
            if existing is not None:
                self._merge_into(existing, ns)
                subjects.append(existing)
            else:
                self.db.add(ns)
                self.db.flush()
                subjects.append(ns)

        if skipped_thin:
            logger.info(
                "Minimum-PII threshold: skipped %d group(s) with name-only records",
                skipped_thin,
            )

        # SQL dedup pass: merge subjects with same canonical_name in the same
        # project.  The Python _find_existing check catches most dupes, but
        # race conditions and different extraction paths can still produce
        # duplicates.  This is the final safety net.
        if subjects:
            project_id = subjects[0].project_id
            if project_id:
                merged = self._sql_dedup(project_id)
                if merged:
                    logger.info("SQL dedup: merged %d duplicate subjects for project %s", merged, project_id)

        return subjects

    def _sql_dedup(self, project_id) -> int:
        """Merge duplicate notification_subjects with same name in same project.

        Keeps the subject with the lowest subject_id (first inserted).
        Combines page ranges before deleting duplicates.
        Returns count of deleted duplicates.
        """
        from sqlalchemy import text as sa_text

        # Find duplicates: same canonical_name, same project
        dupes = self.db.execute(sa_text("""
            SELECT canonical_name, array_agg(subject_id ORDER BY subject_id) as ids
            FROM notification_subjects
            WHERE project_id = :pid AND canonical_name IS NOT NULL
            GROUP BY canonical_name
            HAVING count(*) > 1
        """), {"pid": str(project_id)}).fetchall()

        if not dupes:
            return 0

        total_deleted = 0
        for row in dupes:
            name, ids = row
            keep_id = ids[0]  # keep first
            delete_ids = ids[1:]

            # Merge page ranges from duplicates into the keeper
            for del_id in delete_ids:
                dup = self.db.get(NotificationSubject, del_id)
                keeper = self.db.get(NotificationSubject, keep_id)
                if dup and keeper:
                    self._merge_into(keeper, dup)
                    self.db.delete(dup)
                    total_deleted += 1

        if total_deleted:
            self.db.flush()

        return total_deleted

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_one(self, group: ResolvedGroup) -> NotificationSubject:
        records = group.records

        # --- Canonical name ---
        names = [
            normalize_name(r.raw_name)
            for r in records
            if r.raw_name
        ]
        canonical_name = _best_value(names)

        # --- Canonical email ---
        emails = [r.raw_email.lower() for r in records if r.raw_email]
        canonical_email = _best_value(emails)

        # --- Canonical phone ---
        phones = [r.raw_phone for r in records if r.raw_phone]
        canonical_phone = _best_value(phones)

        # --- Canonical address ---
        addresses = [r.raw_address for r in records if r.raw_address]
        canonical_address = _best_address(addresses)

        # --- PII types (sorted unique) ---
        # Use the FULL entity_types_found from each record (not just the primary
        # entity_type) so that composite records (entity_type="PERSON" but also
        # containing US_SSN, DOB, etc.) propagate all types to the subject.
        # This is critical for protocol triggering — apply_protocol() checks
        # pii_types_found against triggering_entity_types.
        pii_types_set: set[str] = set()
        for r in records:
            if r.entity_types_found:
                pii_types_set.update(r.entity_types_found)
            if r.entity_type:
                pii_types_set.add(r.entity_type)
        pii_types = sorted(pii_types_set)

        # --- Source records ---
        source_records = [r.record_id for r in records]

        # --- Review status ---
        review_status = (
            "HUMAN_REVIEW" if group.needs_human_review else "AI_PENDING"
        )

        # --- Lineage fields (Step 18) ---
        # pii_types_list: pipe-delimited from entity_types_found across all records
        all_entity_types: set[str] = set()
        for r in records:
            all_entity_types.update(r.entity_types_found)
            # Also include the record's own entity_type
            if r.entity_type:
                all_entity_types.add(r.entity_type)
        pii_types_list_str = "|".join(sorted(all_entity_types)) if all_entity_types else None

        # source_page_range: union of page_range values
        page_ranges = [r.page_range for r in records if r.page_range]
        source_page_range = ", ".join(sorted(set(page_ranges))) if page_ranges else None

        # government_id_type: pick the most specific label for the raw ID.
        # Strategy: format-based classifier first (recognises UK_NINO, IN_PAN,
        # BR_CPF, etc. regardless of what the extractor labeled the record as).
        # When the classifier can't narrow the type but a non-US country_hint
        # is present, trust the hint and use GOVERNMENT_ID rather than falling
        # back to the extractor's "US_SSN" placeholder (the extractor labels
        # every gov ID US_SSN regardless of jurisdiction).
        government_id_type: str | None = None
        from app.pii.gov_id_classifier import GENERIC_TYPE, infer_gov_id_type
        for r in records:
            if r.raw_government_id:
                inferred = infer_gov_id_type(r.raw_government_id, country_hint=r.country)
                if inferred != GENERIC_TYPE:
                    government_id_type = inferred
                    break
                # Classifier couldn't narrow it. If we have a non-US hint, the
                # "US_SSN" label the extractor wrote is wrong — don't propagate
                # it. Use the generic label instead.
                country = (r.country or "").upper()
                if country and country != "US":
                    government_id_type = "GOVERNMENT_ID"
                    break
                # Default behaviour: fall back to any specific label the
                # extractor produced.
                if r.entity_types_found:
                    for et in r.entity_types_found:
                        if et.upper() in _GOV_ID_FIELD_TYPES:
                            government_id_type = et
                            break
                if not government_id_type:
                    if r.entity_type.upper() in _GOV_ID_FIELD_TYPES:
                        government_id_type = r.entity_type
                    else:
                        government_id_type = "GOVERNMENT_ID"
                break

        # canonical_dob: best DOB from records, normalized to ISO 8601
        dobs = [r.raw_dob for r in records if r.raw_dob]
        # Normalize all DOBs first, then pick best from normalized values
        normalized_dobs = [normalize_dob(d) for d in dobs]
        normalized_dobs = [d for d in normalized_dobs if d]  # drop None
        if normalized_dobs:
            canonical_dob = _best_value(normalized_dobs)
        else:
            # Fall back to raw value if normalization fails for all
            canonical_dob = _best_value(dobs)

        # canonical_government_id: best government ID from records
        gov_ids = [r.raw_government_id for r in records if r.raw_government_id]
        canonical_government_id = _best_value(gov_ids)

        # extraction_confidence: use group merge_confidence as best proxy
        extraction_confidence = group.merge_confidence

        # source_document_name: look up Document by source_document_id
        source_document_name: str | None = None
        for r in records:
            if r.source_document_id:
                try:
                    from uuid import UUID as _UUID
                    doc = self.db.get(Document, _UUID(r.source_document_id))
                    if doc is not None:
                        source_document_name = doc.file_name
                        break
                except (ValueError, Exception):
                    # source_document_id may be a file path (non-UUID) in full pipeline mode
                    source_document_name = r.source_document_id.rsplit("/", 1)[-1] if "/" in r.source_document_id else r.source_document_id
                    break

        # --- Merge explanation (Step 27) ---
        merge_explanation = None
        if group.merge_explanations:
            merge_explanation = {
                "pairs": [
                    {
                        "record_a_label": ex.record_a_label,
                        "record_b_label": ex.record_b_label,
                        "overall_confidence": ex.overall_confidence,
                        "signals": [
                            {
                                "anchor": s.anchor,
                                "matched": s.matched,
                                "score": s.score,
                                "detail": s.detail,
                                "field_a": s.field_a,
                                "field_b": s.field_b,
                            }
                            for s in ex.signals
                        ],
                    }
                    for ex in group.merge_explanations
                ],
            }

        # Belt-and-suspenders: null out any canonical_* field that matches a
        # known placeholder / prompt-leak string. Task #28 catches these at
        # parse time; this is the second line of defence for cases that
        # slipped through (vision paths, legacy records, etc.).
        from app.pipeline.text_batch_extractor import (
            _is_placeholder_address,
            _is_placeholder_email,
            _is_placeholder_name,
            _is_placeholder_phone,
            _is_placeholder_ssn,
        )

        if canonical_name and _is_placeholder_name(canonical_name):
            logger.debug("Sanity-null canonical_name placeholder: %r", canonical_name[:40])
            canonical_name = None
        if canonical_email and _is_placeholder_email(canonical_email):
            logger.debug("Sanity-null canonical_email placeholder: %r", canonical_email[:40])
            canonical_email = None
        if canonical_phone and _is_placeholder_phone(canonical_phone):
            logger.debug("Sanity-null canonical_phone placeholder: %r", canonical_phone[:40])
            canonical_phone = None
        if canonical_government_id and _is_placeholder_ssn(canonical_government_id):
            logger.debug(
                "Sanity-null canonical_government_id placeholder: %r",
                canonical_government_id[:40],
            )
            canonical_government_id = None
            government_id_type = None  # drop the (now-meaningless) type too
        if canonical_address and isinstance(canonical_address, dict):
            raw_addr = canonical_address.get("raw") if canonical_address else None
            if raw_addr and isinstance(raw_addr, str) and _is_placeholder_address(raw_addr):
                logger.debug("Sanity-null canonical_address placeholder: %r", raw_addr[:40])
                canonical_address = None

        return NotificationSubject(
            subject_id=uuid4(),
            canonical_name=canonical_name,
            canonical_email=canonical_email,
            canonical_address=canonical_address,
            canonical_phone=canonical_phone,
            pii_types_found=pii_types,
            source_records=source_records,
            merge_confidence=group.merge_confidence,
            notification_required=False,
            review_status=review_status,
            source_document_name=source_document_name,
            source_page_range=source_page_range,
            government_id_type=government_id_type,
            extraction_confidence=extraction_confidence,
            pii_types_list=pii_types_list_str,
            canonical_dob=canonical_dob,
            canonical_government_id=canonical_government_id,
            merge_explanation=merge_explanation,
        )

    def _find_existing(self, ns: NotificationSubject) -> NotificationSubject | None:
        """Look up by canonical_email, canonical_phone, or name+gov_id."""
        if ns.canonical_email:
            hit = (
                self.db.query(NotificationSubject)
                .filter(NotificationSubject.canonical_email == ns.canonical_email)
                .first()
            )
            if hit is not None:
                return hit

        if ns.canonical_phone:
            hit = (
                self.db.query(NotificationSubject)
                .filter(NotificationSubject.canonical_phone == ns.canonical_phone)
                .first()
            )
            if hit is not None:
                return hit

        # Match by name + government ID type (same person, different pages)
        if ns.canonical_name and ns.government_id_type and ns.project_id:
            hit = (
                self.db.query(NotificationSubject)
                .filter(
                    NotificationSubject.canonical_name == ns.canonical_name,
                    NotificationSubject.government_id_type == ns.government_id_type,
                    NotificationSubject.project_id == ns.project_id,
                )
                .first()
            )
            if hit is not None:
                return hit

        # Match by name only within same project (no gov ID but same person)
        if ns.canonical_name and ns.project_id:
            hit = (
                self.db.query(NotificationSubject)
                .filter(
                    NotificationSubject.canonical_name == ns.canonical_name,
                    NotificationSubject.project_id == ns.project_id,
                )
                .first()
            )
            if hit is not None:
                return hit

        return None

    @staticmethod
    def _merge_into(
        existing: NotificationSubject,
        incoming: NotificationSubject,
    ) -> None:
        """Merge *incoming* fields into *existing* in place."""
        # Union pii_types_found
        old_types = set(existing.pii_types_found or [])
        new_types = set(incoming.pii_types_found or [])
        existing.pii_types_found = sorted(old_types | new_types)

        # Append source_records (dedup)
        old_recs = list(existing.source_records or [])
        new_recs = list(incoming.source_records or [])
        seen = set(old_recs)
        for r in new_recs:
            if r not in seen:
                old_recs.append(r)
                seen.add(r)
        existing.source_records = old_recs

        # Merge page ranges
        old_pages = set((existing.source_page_range or "").split(", "))
        new_pages = set((incoming.source_page_range or "").split(", "))
        merged_pages = sorted(p for p in old_pages | new_pages if p)
        if merged_pages:
            existing.source_page_range = ", ".join(merged_pages)

        # Fill in missing fields from incoming
        if not existing.canonical_name and incoming.canonical_name:
            existing.canonical_name = incoming.canonical_name
        if not existing.canonical_email and incoming.canonical_email:
            existing.canonical_email = incoming.canonical_email
        if not existing.canonical_phone and incoming.canonical_phone:
            existing.canonical_phone = incoming.canonical_phone
        if not existing.canonical_address and incoming.canonical_address:
            existing.canonical_address = incoming.canonical_address
        if not existing.government_id_type and incoming.government_id_type:
            existing.government_id_type = incoming.government_id_type
        if incoming.notification_required:
            existing.notification_required = True

        # Keep lower merge_confidence (more conservative)
        inc_conf = incoming.merge_confidence if incoming.merge_confidence is not None else 1.0
        ext_conf = existing.merge_confidence if existing.merge_confidence is not None else 1.0
        existing.merge_confidence = min(ext_conf, inc_conf)
