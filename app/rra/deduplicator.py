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

from collections import Counter
from uuid import uuid4

from sqlalchemy.orm import Session

from app.db.models import NotificationSubject
from app.normalization.name_normalizer import normalize_name
from app.rra.entity_resolver import PIIRecord, ResolvedGroup


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


def _best_address(addresses: list[dict | None]) -> dict | None:
    """Pick canonical address: most frequent postal code wins."""
    valid = [a for a in addresses if a]
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

        for group in groups:
            ns = self._build_one(group)
            existing = self._find_existing(ns)
            if existing is not None:
                self._merge_into(existing, ns)
                subjects.append(existing)
            else:
                self.db.add(ns)
                self.db.flush()
                subjects.append(ns)
        return subjects

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
        pii_types = sorted({r.entity_type for r in records})

        # --- Source records ---
        source_records = [r.record_id for r in records]

        # --- Review status ---
        review_status = (
            "HUMAN_REVIEW" if group.needs_human_review else "AI_PENDING"
        )

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
        )

    def _find_existing(self, ns: NotificationSubject) -> NotificationSubject | None:
        """Look up by canonical_email first, then canonical_phone."""
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

        # Keep lower merge_confidence (more conservative)
        inc_conf = incoming.merge_confidence if incoming.merge_confidence is not None else 1.0
        ext_conf = existing.merge_confidence if existing.merge_confidence is not None else 1.0
        existing.merge_confidence = min(ext_conf, inc_conf)
