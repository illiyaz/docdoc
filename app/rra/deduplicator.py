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
# PLUS one of these corroborating PII types. Name-only records are noise
# and should not generate notification subjects.
#
# Source of truth: gov_id_classifier.SUPPORTED_TYPES owns the set of
# government ID types (50+ across 35+ countries). This file adds non-
# gov-ID corroborating types (DOB, email, phone, address) and industry-
# specific identifiers (medical, education, HR, insurance, legal) that
# the classifier doesn't track.
#
# The two sources stay in sync because _CORROBORATING_PII_TYPES is
# built at import time from the classifier + local additions. Add a
# new country's gov ID to gov_id_classifier and it flows here
# automatically. Non-gov-ID corroborating fields stay local to this
# module.
from app.pii.gov_id_classifier import EXPANDED_KNOWN_TYPES as _GOV_ID_TYPES

# Non-gov-ID corroborating fields — aliases and industry-specific IDs.
# These aren't government-issued but uniquely identify a person in
# their context (patient in EHR, employee in HR, student in academic,
# policyholder in insurance, etc.).
_NON_GOV_CORROBORATING = frozenset({
    # Direct contact / demographic
    "DATE_OF_BIRTH", "DATE_OF_BIRTH_MDY", "DATE_OF_BIRTH_DMY",
    "EMAIL_ADDRESS", "EMAIL",
    "PHONE_NUMBER", "PHONE_US", "PHONE_INTL",
    "LOCATION", "ADDRESS",
    # Biometric
    "BIOMETRIC",
    # Healthcare identifiers (distinct from gov IDs)
    "PHI_MRN", "MEDICAL_RECORD", "MRN",      # different labels same concept
    "PHI_NPI", "NPI_NUMBER", "MEDICAL_LICENSE",
    "PATIENT_ID", "PROVIDER_ID", "INSURANCE_ID", "PAYER_ID",
    "MEDICARE_NUMBER", "MEDICAID_NUMBER",
    # Employment / HR
    "EMPLOYEE_ID", "EMPLOYER_ID", "BADGE_ID", "ACCESS_CARD",
    "KEY_FOB_ID", "STAFF_ID",
    # Education (FERPA)
    "STUDENT_ID", "ENROLLMENT_ID", "STUDENT_EMAIL",
    # Insurance
    "POLICY_NUMBER", "POLICYHOLDER_ID", "CLAIM_NUMBER",
    "GROUP_NUMBER", "SUBSCRIBER_ID",
    # Legal
    "CASE_NUMBER", "DOCKET_NUMBER", "COURT_CASE",
    # Finance (beyond gov-issued tax IDs)
    "CREDIT_CARD", "BANK_ACCOUNT", "US_BANK_NUMBER", "US_BANK_ROUTING",
    "ACCOUNT_NUMBER", "ACCOUNT_REFERENCE", "IBAN_CODE",
    "MEMBER_ID", "LOAN_ID", "MORTGAGE_ID",
    # Telecom / SaaS
    "CUSTOMER_ID", "SUBSCRIPTION_ID", "USER_ID",
    # Government / civic (beyond gov IDs)
    "VOTER_ID", "SSA_CLAIM_NUMBER",
})

_CORROBORATING_PII_TYPES = _GOV_ID_TYPES | _NON_GOV_CORROBORATING

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


def _gov_id_match_key(gov_id: str | None) -> str:
    """Return a mask-variant-insensitive key for a government ID.

    Same person on two pages may have their SSN written as ``XXX-XX-2682``
    on one and ``XXXXX2682`` on another. This key strips mask characters
    and punctuation so both normalize to ``2682`` (or the full trailing
    alphanumeric run for strict formats like UK_NINO ``YB146386C``).

    Returns empty string for values too short / placeholder / unusable.
    """
    if not gov_id:
        return ""
    import re as _re
    s = str(gov_id).strip().upper()
    if not s:
        return ""
    # Strip mask chars + whitespace + punctuation
    stripped = _re.sub(r"[X*#\s\-\._]", "", s)
    if not stripped or len(stripped) < 4:
        return ""
    # Pure-digit → last 4 (US SSN / NL BSN / IL ID / most 9-digit formats).
    if stripped.isdigit():
        return stripped[-4:]
    # Alphanumeric → use full normalized string (UK_NINO, IN_PAN, etc.
    # where every character is load-bearing).
    return stripped


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

    def __init__(self, db_session: Session, project_id=None) -> None:
        self.db = db_session
        # project_id is plumbed in so in-memory _find_existing + SQL
        # dedup can actually run — previously the caller set project_id
        # AFTER build_subjects() returned, leaving every dedup check
        # short-circuited (ns.project_id was None). This caused same-
        # person duplicates like Katelyn Cook × 3 in the verify run.
        self.project_id = project_id

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
            # Minimum-PII threshold (BIG_FIXES #H1): skip groups that have
            # only a name and no corroborating PII. "Corroborating" is the
            # union of:
            #   - standard contact fields (DOB, email, phone, address)
            #   - all known gov-ID types (from gov_id_classifier)
            #   - industry-specific IDs (MRN, student_id, badge_id, etc.)
            #   - anything the doc's own segregation contract declared
            #     as an expected field (so new doc types self-configure)
            has_corroboration = False
            has_name = False
            all_entity_types: set[str] = set()

            # Per-group doc contract: the union of field_inventory types
            # across every record in the group. Adaptive — a doc type we
            # haven't seen before (new geography, new industry) still
            # passes the filter as long as segregation classified its
            # fields as expected PII.
            contract_types: set[str] = set()
            for r in group.records:
                fc = getattr(r, "field_contract", None)
                if fc:
                    contract_types.update(t.upper() for t in fc if t)

            # Merge the static allowlist with this group's contract.
            effective_corroborating = _CORROBORATING_PII_TYPES | contract_types

            for r in group.records:
                if r.raw_name:
                    has_name = True
                if r.raw_email or r.raw_phone or r.raw_dob or r.raw_address or r.raw_government_id:
                    has_corroboration = True
                # Collect all entity types for low-value check
                if r.entity_types_found:
                    all_entity_types.update(et.upper() for et in r.entity_types_found)
                    for et in r.entity_types_found:
                        if et.upper() in effective_corroborating:
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

        # Find duplicates: same canonical_name AND matching gov-ID key
        # (mask-variant aware). Candidates come out of SQL; Python
        # partitions them into buckets using _gov_id_match_key so a
        # family of distinct people sharing a surname but with different
        # gov IDs stay separate (BIG_FIXES #B2).
        rows = self.db.execute(sa_text("""
            SELECT canonical_name,
                   subject_id,
                   canonical_government_id
            FROM notification_subjects
            WHERE project_id = :pid AND canonical_name IS NOT NULL
            ORDER BY canonical_name, subject_id
        """), {"pid": str(project_id)}).fetchall()

        # Group by (canonical_name, gov_id_match_key). Records with a
        # gov_id go into their own bucket; records with no gov_id share
        # one bucket per name so they still merge together.
        from collections import defaultdict as _dd
        buckets: dict[tuple[str, str], list] = _dd(list)
        for name, subj_id, gov_id in rows:
            key = (name, _gov_id_match_key(gov_id))
            buckets[key].append(subj_id)

        dupes = [(key, ids) for key, ids in buckets.items() if len(ids) > 1]
        if not dupes:
            return 0

        total_deleted = 0
        for (name, _key), ids in dupes:
            keep_id = ids[0]
            delete_ids = ids[1:]

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
        #
        # I2 (BIG_FIXES): normalise domain labels (MEDICAL_RECORD,
        # STUDENT_ID, etc.) to the protocol-match form (PHI_MRN,
        # FERPA_STUDENT_ID) so HIPAA/HITECH/FERPA actually trigger.
        # Keeps the original label too for doc-type observability.
        from app.pii.gov_id_classifier import normalize_protocol_label
        pii_types_set: set[str] = set()
        for r in records:
            if r.entity_types_found:
                for t in r.entity_types_found:
                    pii_types_set.add(t)
                    normalised = normalize_protocol_label(t)
                    if normalised and normalised != t:
                        pii_types_set.add(normalised)
            if r.entity_type:
                pii_types_set.add(r.entity_type)
                normalised = normalize_protocol_label(r.entity_type)
                if normalised and normalised != r.entity_type:
                    pii_types_set.add(normalised)
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
        # I6 + I8: build the "contract" for the classifier.
        # I6 originally used r.entity_types_found (what the extractor
        # emitted — narrow, mostly PERSON + US_SSN).
        # I8 (BIG_FIXES): authoritative source is segregation's
        # field_inventory on the source document. Badge logs with
        # EMPLOYEE_ID fields got mislabelled US_SSN because the
        # extractor never saw EMPLOYEE_ID — only segregation did.
        # Union the two sources so we get the full picture:
        #   segregation.field_inventory (doc-level, canonical contract)
        #   ∪ records' entity_types_found (what ran through extraction)
        _contract_types: list[str] = []
        _contract_doc_type: str | None = None   # I10: for classifier tiebreak
        for r in records:
            if r.entity_types_found:
                _contract_types.extend(r.entity_types_found)
        # Pull segregation contract per source_document_id, unique
        _seen_doc_ids: set[str] = set()
        for r in records:
            _sdi = str(r.source_document_id or "")
            if not _sdi or _sdi in _seen_doc_ids:
                continue
            _seen_doc_ids.add(_sdi)
            try:
                from app.db.models import Document as _Doc
                from uuid import UUID as _UUID
                _doc = None
                try:
                    _doc = self.db.get(_Doc, _UUID(_sdi))
                except (ValueError, TypeError):
                    _doc = None
                if _doc is not None and _doc.metadata_json:
                    _seg = _doc.metadata_json.get("segregation") if isinstance(_doc.metadata_json, dict) else None
                    if isinstance(_seg, dict):
                        _fi = _seg.get("field_inventory") or []
                        _contract_types.extend(str(t) for t in _fi if t)
                        # I10: capture doc_type for classifier tiebreak
                        if _contract_doc_type is None:
                            _dtype = _seg.get("document_type")
                            if _dtype:
                                _contract_doc_type = str(_dtype)
            except Exception:
                pass  # segregation lookup is best-effort
        for r in records:
            if r.raw_government_id:
                inferred = infer_gov_id_type(
                    r.raw_government_id,
                    country_hint=r.country,
                    contract_field_types=_contract_types or None,
                    document_type=_contract_doc_type,
                )
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

        # canonical_dob: best DOB from records, normalized to ISO 8601.
        # Pass each record's country hint so DD/MM vs MM/DD disambiguation
        # respects the document's locale (BIG_FIXES #C2).
        dob_pairs = [(r.raw_dob, getattr(r, "country", None)) for r in records if r.raw_dob]
        dobs = [d for d, _ in dob_pairs]
        normalized_dobs = [normalize_dob(d, country=c) for d, c in dob_pairs]
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
            project_id=self.project_id,  # plumbed in so dedup can fire
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
        """Look up by canonical_email, canonical_phone, or name+gov_id.

        Guards against two previously-seen bugs (BIG_FIXES #B1, #B2):

        B1 — mask-format variants: ``XXXXX2682`` and ``XXX-XX-2682`` are
        the same SSN, so match uses :func:`_gov_id_match_key` (last-4
        digits for numeric IDs, uppercase alphanumeric for strict IDs).
        B2 — surname collapse: only merge on name-alone when neither
        record has a gov ID. If both have gov IDs and they differ, these
        are distinct people (e.g. 3 Bhudia siblings with distinct NIs).
        """
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

        # Match by name + same gov-ID value (mask-variant aware).
        # Query by name/type then filter by normalized-gov-id in Python —
        # the normalization isn't expressible as a pure SQL predicate.
        ns_key = _gov_id_match_key(ns.canonical_government_id)
        if ns.canonical_name and ns.project_id and ns_key:
            candidates = (
                self.db.query(NotificationSubject)
                .filter(
                    NotificationSubject.canonical_name == ns.canonical_name,
                    NotificationSubject.project_id == ns.project_id,
                )
                .all()
            )
            for cand in candidates:
                cand_key = _gov_id_match_key(cand.canonical_government_id)
                if cand_key and cand_key == ns_key:
                    return cand

        # Match by name only — but ONLY when neither side has a gov ID.
        # Prevents surname-collapse between distinct people sharing a
        # surname where each has a different gov ID.
        if ns.canonical_name and ns.project_id and not ns.canonical_government_id:
            hit = (
                self.db.query(NotificationSubject)
                .filter(
                    NotificationSubject.canonical_name == ns.canonical_name,
                    NotificationSubject.project_id == ns.project_id,
                    NotificationSubject.canonical_government_id.is_(None),
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
        # I9 (BIG_FIXES): upgrade government_id_type when incoming is
        # more specific. Without this, once a subject gets created with
        # the wrong label (e.g. pre-I8 pipeline tagged an EMPLOYEE_ID
        # value as US_SSN), no amount of re-extraction can fix it.
        # Precedence: specific institutional / protocol-aligned >
        # canonical gov ID > generic.
        def _type_rank(t: str | None) -> int:
            if not t:
                return 0
            u = t.strip().upper()
            # Tier 3 — institutional / protocol-aligned (FERPA_STUDENT_ID,
            # PHI_MRN, PHI_NPI, EMPLOYEE_ID, etc.) — highest confidence
            # that the doc's context maps the value correctly
            tier3 = {
                "FERPA_STUDENT_ID", "PHI_MRN", "PHI_NPI", "PHI_HEALTH_PLAN",
                "PHI_DEA", "PHI_HICN", "PHI_ICD10", "US_MEDICARE_MBI",
                "EMPLOYEE_ID", "BADGE_ID", "STUDENT_ID",
                "POLICY_NUMBER", "CASE_NUMBER",
            }
            if u in tier3:
                return 3
            # Tier 2 — specific gov ID type (from classifier)
            tier2 = {"US_SSN", "US_DRIVER_LICENSE", "US_PASSPORT",
                     "UK_NINO", "IN_AADHAAR", "IN_PAN", "BR_CPF",
                     "CA_SIN", "DE_IDNR", "FR_NIR"}
            if u in tier2:
                return 2
            # Tier 1 — country-specific but less common, or alias labels
            if "_" in u and len(u) <= 20:
                return 2
            # Tier 0 — generic/unknown fallback
            return 1
        if incoming.government_id_type:
            incoming_rank = _type_rank(incoming.government_id_type)
            existing_rank = _type_rank(existing.government_id_type)
            if incoming_rank > existing_rank:
                existing.government_id_type = incoming.government_id_type
        if incoming.notification_required:
            existing.notification_required = True

        # Keep lower merge_confidence (more conservative)
        inc_conf = incoming.merge_confidence if incoming.merge_confidence is not None else 1.0
        ext_conf = existing.merge_confidence if existing.merge_confidence is not None else 1.0
        existing.merge_confidence = min(ext_conf, inc_conf)
