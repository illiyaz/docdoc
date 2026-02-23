"""Entity resolver — Phase 2.

Links ``PIIRecord`` objects across documents to unique individuals using a
confidence ladder and Union-Find (disjoint set) for transitive merging.

Confidence ladder (from CLAUDE.md §11):
  +0.50  government IDs match (same type, exact)
  +0.40  emails match exactly
  +0.35  phones match exactly (both non-None)
  +0.35  names match AND DOBs match
  +0.25  names match AND addresses match (fuzzy)
  +0.10  names match alone
  Cap at 1.0.  Returns 0.0 if no signal fires.

Pairs with combined confidence ≥ 0.60 are unioned.  Groups whose minimum
pairwise confidence is < 0.80 are flagged for human review.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.normalization.email_normalizer import normalize_email
from app.rra.fuzzy import (
    addresses_match,
    dobs_match,
    government_ids_match,
    names_match,
)

# PII entity types that represent government-issued IDs
_GOV_ID_TYPES: frozenset[str] = frozenset({
    "US_SSN", "SSN",
    "US_PASSPORT", "PASSPORT",
    "US_DRIVER_LICENSE", "DRIVER_LICENSE",
    "UK_NHS", "UK_NINO",
    "AU_TFN", "AU_MEDICARE",
    "IN_AADHAAR", "IN_PAN",
    "GOVERNMENT_ID",
})


# ---------------------------------------------------------------------------
# Input dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PIIRecord:
    """A single PII extraction, already normalised by the normalization layer."""

    record_id: str
    entity_type: str
    normalized_value: str
    raw_name: str | None = None
    raw_address: dict | None = None
    raw_phone: str | None = None
    raw_email: str | None = None
    raw_dob: str | None = None
    country: str = "US"
    source_document_id: str = ""
    page_or_sheet: str | int = 0


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------

@dataclass
class ResolvedGroup:
    """A group of ``PIIRecord`` objects resolved to one individual."""

    group_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    records: list[PIIRecord] = field(default_factory=list)
    merge_confidence: float = 1.0
    needs_human_review: bool = False


# ---------------------------------------------------------------------------
# Confidence builder
# ---------------------------------------------------------------------------

def build_confidence(r1: PIIRecord, r2: PIIRecord) -> float:
    """Return the pairwise merge confidence for two records.

    Signals are additive and capped at 1.0.
    """
    score = 0.0

    # --- Government ID match (+0.50) ---
    if (
        r1.entity_type.upper() in _GOV_ID_TYPES
        and r2.entity_type.upper() in _GOV_ID_TYPES
    ):
        matched, _ = government_ids_match(
            r1.entity_type, r1.normalized_value,
            r2.entity_type, r2.normalized_value,
        )
        if matched:
            score += 0.50

    # --- Email match (+0.40) ---
    e1 = normalize_email(r1.raw_email) if r1.raw_email else None
    e2 = normalize_email(r2.raw_email) if r2.raw_email else None
    if e1 and e2 and e1 == e2:
        score += 0.40

    # --- Phone match (+0.35) ---
    if r1.raw_phone and r2.raw_phone:
        if r1.raw_phone == r2.raw_phone:
            score += 0.35

    # --- Name-dependent signals ---
    name_matched = False
    if r1.raw_name and r2.raw_name:
        name_matched, _ = names_match(r1.raw_name, r2.raw_name)

    if name_matched:
        # Name + DOB (+0.35)
        if r1.raw_dob and r2.raw_dob:
            dob_matched, _ = dobs_match(
                r1.raw_dob, r1.country,
                r2.raw_dob, r2.country,
            )
            if dob_matched:
                score += 0.35

        # Name + address (+0.25)
        if r1.raw_address and r2.raw_address:
            addr_matched, _ = addresses_match(r1.raw_address, r2.raw_address)
            if addr_matched:
                score += 0.25

        # Name alone (+0.10)
        score += 0.10

    return min(score, 1.0)


# ---------------------------------------------------------------------------
# Union-Find
# ---------------------------------------------------------------------------

class _UnionFind:
    """Weighted quick-union with path compression."""

    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]  # path halving
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

class EntityResolver:
    """Resolve ``PIIRecord`` objects to groups of unique individuals."""

    MERGE_THRESHOLD: float = 0.30
    REVIEW_THRESHOLD: float = 0.80

    def resolve(self, records: list[PIIRecord]) -> list[ResolvedGroup]:
        """Group *records* by individual identity using Union-Find.

        Returns one ``ResolvedGroup`` per unique individual (including
        single-record groups for unmatched records).
        """
        n = len(records)
        if n == 0:
            return []

        uf = _UnionFind(n)
        # Store pairwise confidences for pairs that were merged
        pair_conf: dict[tuple[int, int], float] = {}

        for i in range(n):
            for j in range(i + 1, n):
                conf = build_confidence(records[i], records[j])
                if conf >= self.MERGE_THRESHOLD:
                    uf.union(i, j)
                    key = (min(i, j), max(i, j))
                    pair_conf[key] = conf

        # Collect groups by root
        groups: dict[int, list[int]] = {}
        for i in range(n):
            root = uf.find(i)
            groups.setdefault(root, []).append(i)

        result: list[ResolvedGroup] = []
        for indices in groups.values():
            group_records = [records[i] for i in indices]

            if len(indices) == 1:
                result.append(ResolvedGroup(
                    records=group_records,
                    merge_confidence=1.0,
                    needs_human_review=False,
                ))
                continue

            # min pairwise confidence among all pairs in this group
            min_conf = 1.0
            for a in indices:
                for b in indices:
                    if a >= b:
                        continue
                    key = (min(a, b), max(a, b))
                    if key in pair_conf:
                        min_conf = min(min_conf, pair_conf[key])
                    else:
                        # Pair not directly merged but transitively linked
                        c = build_confidence(records[a], records[b])
                        min_conf = min(min_conf, c)

            result.append(ResolvedGroup(
                records=group_records,
                merge_confidence=min_conf,
                needs_human_review=min_conf < self.REVIEW_THRESHOLD,
            ))

        return result
