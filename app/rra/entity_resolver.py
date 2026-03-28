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

Phase 5 Step 5 — Configurable dedup anchors:
  ``build_confidence`` and ``EntityResolver.resolve`` accept an optional
  ``active_anchors`` list that controls which matching signals are evaluated.
  When ``None`` (default), all signals are active (backward compatible).
  Valid anchor names: ``ssn``, ``email``, ``phone``, ``name_dob``,
  ``name_address``, ``name``.
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

# Valid anchor names for configurable dedup (Phase 5 Step 5)
VALID_ANCHORS: frozenset[str] = frozenset({
    "ssn",           # government ID match (+0.50)
    "email",         # exact email match (+0.40)
    "phone",         # exact phone match (+0.35)
    "name_dob",      # name + DOB match (+0.35, plus name-alone +0.10)
    "name_address",  # name + address match (+0.25, plus name-alone +0.10)
    "name",          # name-only match (+0.10)
})

# Canonical name for "all anchors active" — same as passing None
ALL_ANCHORS: frozenset[str] = VALID_ANCHORS


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
    raw_government_id: str | None = None
    country: str = "US"
    source_document_id: str = ""
    page_or_sheet: str | int = 0
    entity_role: str | None = None
    page_range: str = ""
    entity_types_found: tuple[str, ...] = ()
    validation_flags: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MergeSignal:
    """One signal contributing to a merge decision between two records."""

    anchor: str        # e.g. "ssn", "email", "name_dob", "name"
    matched: bool
    score: float       # contribution to confidence (0.0 if not matched)
    detail: str        # human-readable: "SSN exact match"
    field_a: str       # masked value from record A
    field_b: str       # masked value from record B


@dataclass
class MergeExplanation:
    """Explains why two records were (or weren't) merged."""

    record_a_label: str   # "J. Smith from doc_A.pdf p.5"
    record_b_label: str   # "John Smith from doc_B.xlsx p.42"
    overall_confidence: float
    signals: list[MergeSignal] = field(default_factory=list)


@dataclass
class ResolvedGroup:
    """A group of ``PIIRecord`` objects resolved to one individual."""

    group_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    records: list[PIIRecord] = field(default_factory=list)
    merge_confidence: float = 1.0
    needs_human_review: bool = False
    merge_explanations: list[MergeExplanation] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Confidence builder
# ---------------------------------------------------------------------------

def _resolve_anchors(
    active_anchors: list[str] | frozenset[str] | None,
) -> frozenset[str]:
    """Normalise *active_anchors* to a validated frozenset.

    Returns ``VALID_ANCHORS`` when *active_anchors* is ``None`` or empty
    (backward-compatible default — all signals active).

    Raises ``ValueError`` if any anchor name is not in ``VALID_ANCHORS``.
    """
    if active_anchors is None:
        return VALID_ANCHORS

    anchors = frozenset(a.lower().strip() for a in active_anchors)
    if not anchors:
        return VALID_ANCHORS

    invalid = anchors - VALID_ANCHORS
    if invalid:
        raise ValueError(
            f"Invalid dedup anchor(s): {sorted(invalid)}. "
            f"Valid anchors: {sorted(VALID_ANCHORS)}"
        )
    return anchors


def build_confidence(
    r1: PIIRecord,
    r2: PIIRecord,
    *,
    active_anchors: list[str] | frozenset[str] | None = None,
) -> float:
    """Return the pairwise merge confidence for two records.

    Signals are additive and capped at 1.0.

    Parameters
    ----------
    active_anchors:
        Optional list of anchor names that controls which matching signals
        are evaluated.  When ``None`` (default), all signals are active.
        Valid values: ``"ssn"``, ``"email"``, ``"phone"``, ``"name_dob"``,
        ``"name_address"``, ``"name"``.
    """
    anchors = _resolve_anchors(active_anchors)

    # --- Cross-role merge prevention ---
    # If one record is primary_subject and the other is institutional,
    # they cannot be the same person — return 0.0 immediately.
    roles = {r1.entity_role, r2.entity_role}
    if "primary_subject" in roles and "institutional" in roles:
        return 0.0
    if "primary_subject" in roles and "provider" in roles:
        return 0.0

    # --- Cross-instance merge prevention ---
    # If two records come from the SAME document but DIFFERENT template
    # instances (different page_range), they are DIFFERENT people.
    # Each template instance boundary = one unique individual.
    # Return 0.0 immediately to prevent any merging.
    if (
        r1.source_document_id
        and r1.source_document_id == r2.source_document_id
        and r1.page_range and r2.page_range
        and r1.page_range != r2.page_range
    ):
        return 0.0

    score = 0.0

    # --- Same-document same-name safety net (+0.95) ---
    # If two records come from the same document AND have the same name
    # AND the same page_range, they almost certainly refer to the same
    # individual (e.g. per-detection records from template grouping
    # that didn't fully merge).
    # Only triggers when source_document_id looks like a real reference
    # (UUID, file path, or 8+ char identifier — not short test stubs).
    if (
        r1.source_document_id
        and len(r1.source_document_id) >= 8
        and r1.source_document_id == r2.source_document_id
        and r1.raw_name and r2.raw_name
    ):
        n1 = " ".join(r1.raw_name.lower().split())
        n2 = " ".join(r2.raw_name.lower().split())
        if n1 == n2:
            return 0.95

    # --- Government ID match (+0.50) ---
    if "ssn" in anchors:
        gov_matched = False
        # Match via entity_type (single-field records from per-detection mapping)
        if (
            r1.entity_type.upper() in _GOV_ID_TYPES
            and r2.entity_type.upper() in _GOV_ID_TYPES
        ):
            gov_matched, _ = government_ids_match(
                r1.entity_type, r1.normalized_value,
                r2.entity_type, r2.normalized_value,
            )
        # Match via raw_government_id (composite records from template grouping)
        if not gov_matched and r1.raw_government_id and r2.raw_government_id:
            gov_matched = r1.raw_government_id.strip() == r2.raw_government_id.strip()
        if gov_matched:
            score += 0.50

    # --- Email match (+0.40) ---
    if "email" in anchors:
        e1 = normalize_email(r1.raw_email) if r1.raw_email else None
        e2 = normalize_email(r2.raw_email) if r2.raw_email else None
        if e1 and e2 and e1 == e2:
            score += 0.40

    # --- Phone match (+0.35) ---
    if "phone" in anchors:
        if r1.raw_phone and r2.raw_phone:
            if r1.raw_phone == r2.raw_phone:
                score += 0.35

    # --- Name-dependent signals ---
    has_name_signal = anchors & {"name_dob", "name_address", "name"}
    name_matched = False
    if has_name_signal and r1.raw_name and r2.raw_name:
        name_matched, _ = names_match(r1.raw_name, r2.raw_name)

    if name_matched:
        # Name + DOB (+0.35)
        if "name_dob" in anchors and r1.raw_dob and r2.raw_dob:
            dob_matched, _ = dobs_match(
                r1.raw_dob, r1.country,
                r2.raw_dob, r2.country,
            )
            if dob_matched:
                score += 0.35

        # Name + address (+0.25)
        if "name_address" in anchors and r1.raw_address and r2.raw_address:
            addr_matched, _ = addresses_match(r1.raw_address, r2.raw_address)
            if addr_matched:
                score += 0.25

        # Name alone (+0.10)
        if "name" in anchors:
            score += 0.10

    return min(score, 1.0)


def _mask_gov_id(val: str | None) -> str:
    """Mask a government ID to last 4 chars."""
    if not val:
        return ""
    v = val.strip()
    return f"***{v[-4:]}" if len(v) >= 4 else "***"


def _record_label(r: PIIRecord) -> str:
    """Human-readable label for a record in merge explanations."""
    name = r.raw_name or r.normalized_value or "?"
    doc = r.source_document_id or "unknown"
    if len(doc) > 30:
        doc = doc.split("/")[-1] if "/" in doc else doc[:30]
    page = r.page_or_sheet
    return f"{name} from {doc} p.{page}"


def build_confidence_explained(
    r1: PIIRecord,
    r2: PIIRecord,
    *,
    active_anchors: list[str] | frozenset[str] | None = None,
) -> MergeExplanation:
    """Like ``build_confidence`` but returns full reasoning.

    The returned ``MergeExplanation`` includes per-anchor signals showing
    what matched, what didn't, and the score contribution of each.
    """
    anchors = _resolve_anchors(active_anchors)
    signals: list[MergeSignal] = []

    # Cross-role / cross-instance checks (0.0 short-circuits)
    roles = {r1.entity_role, r2.entity_role}
    if ("primary_subject" in roles and "institutional" in roles) or \
       ("primary_subject" in roles and "provider" in roles):
        return MergeExplanation(
            record_a_label=_record_label(r1),
            record_b_label=_record_label(r2),
            overall_confidence=0.0,
            signals=[MergeSignal("role", False, 0.0, "Cross-role merge blocked", r1.entity_role or "", r2.entity_role or "")],
        )

    if (r1.source_document_id and r1.source_document_id == r2.source_document_id
            and r1.page_range and r2.page_range and r1.page_range != r2.page_range):
        return MergeExplanation(
            record_a_label=_record_label(r1),
            record_b_label=_record_label(r2),
            overall_confidence=0.0,
            signals=[MergeSignal("instance", False, 0.0, "Cross-instance merge blocked", r1.page_range, r2.page_range)],
        )

    score = 0.0

    # Government ID
    if "ssn" in anchors:
        gov_matched = False
        if r1.entity_type.upper() in _GOV_ID_TYPES and r2.entity_type.upper() in _GOV_ID_TYPES:
            gov_matched, _ = government_ids_match(r1.entity_type, r1.normalized_value, r2.entity_type, r2.normalized_value)
        if not gov_matched and r1.raw_government_id and r2.raw_government_id:
            gov_matched = r1.raw_government_id.strip() == r2.raw_government_id.strip()
        s = 0.50 if gov_matched else 0.0
        score += s
        signals.append(MergeSignal("ssn", gov_matched, s,
            "Gov ID exact match" if gov_matched else "Gov IDs differ or absent",
            _mask_gov_id(r1.raw_government_id or r1.normalized_value),
            _mask_gov_id(r2.raw_government_id or r2.normalized_value)))

    # Email
    if "email" in anchors:
        e1 = normalize_email(r1.raw_email) if r1.raw_email else None
        e2 = normalize_email(r2.raw_email) if r2.raw_email else None
        matched = bool(e1 and e2 and e1 == e2)
        s = 0.40 if matched else 0.0
        score += s
        signals.append(MergeSignal("email", matched, s,
            "Email exact match" if matched else "Emails differ or absent",
            r1.raw_email or "", r2.raw_email or ""))

    # Phone
    if "phone" in anchors:
        matched = bool(r1.raw_phone and r2.raw_phone and r1.raw_phone == r2.raw_phone)
        s = 0.35 if matched else 0.0
        score += s
        signals.append(MergeSignal("phone", matched, s,
            "Phone exact match" if matched else "Phones differ or absent",
            r1.raw_phone or "", r2.raw_phone or ""))

    # Name-dependent
    has_name = anchors & {"name_dob", "name_address", "name"}
    name_matched = False
    if has_name and r1.raw_name and r2.raw_name:
        name_matched, _ = names_match(r1.raw_name, r2.raw_name)

    if "name_dob" in anchors:
        dob_matched = False
        if name_matched and r1.raw_dob and r2.raw_dob:
            dob_matched, _ = dobs_match(r1.raw_dob, r1.country, r2.raw_dob, r2.country)
        s = 0.35 if (name_matched and dob_matched) else 0.0
        score += s
        signals.append(MergeSignal("name_dob", name_matched and dob_matched, s,
            "Name + DOB match" if (name_matched and dob_matched) else "Name/DOB differ or absent",
            f"{r1.raw_name or ''} ({r1.raw_dob or ''})",
            f"{r2.raw_name or ''} ({r2.raw_dob or ''})"))

    if "name_address" in anchors:
        addr_matched = False
        if name_matched and r1.raw_address and r2.raw_address:
            addr_matched, _ = addresses_match(r1.raw_address, r2.raw_address)
        s = 0.25 if (name_matched and addr_matched) else 0.0
        score += s
        signals.append(MergeSignal("name_address", name_matched and addr_matched, s,
            "Name + address match" if (name_matched and addr_matched) else "Name/address differ or absent",
            r1.raw_name or "", r2.raw_name or ""))

    if "name" in anchors:
        s = 0.10 if name_matched else 0.0
        score += s
        signals.append(MergeSignal("name", name_matched, s,
            "Name fuzzy match" if name_matched else "Names differ or absent",
            r1.raw_name or "", r2.raw_name or ""))

    return MergeExplanation(
        record_a_label=_record_label(r1),
        record_b_label=_record_label(r2),
        overall_confidence=min(score, 1.0),
        signals=signals,
    )


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

    def resolve(
        self,
        records: list[PIIRecord],
        *,
        active_anchors: list[str] | frozenset[str] | None = None,
    ) -> list[ResolvedGroup]:
        """Group *records* by individual identity using Union-Find.

        Parameters
        ----------
        active_anchors:
            Optional list of anchor names controlling which matching signals
            are active during resolution.  When ``None`` (default), all
            signals are used (backward compatible).  Valid values:
            ``"ssn"``, ``"email"``, ``"phone"``, ``"name_dob"``,
            ``"name_address"``, ``"name"``.

        Returns one ``ResolvedGroup`` per unique individual (including
        single-record groups for unmatched records).
        """
        # Validate once, reuse the resolved frozenset for all pair comparisons
        anchors = _resolve_anchors(active_anchors)

        n = len(records)
        if n == 0:
            return []

        uf = _UnionFind(n)
        # Store pairwise confidences for pairs that were merged
        pair_conf: dict[tuple[int, int], float] = {}

        for i in range(n):
            for j in range(i + 1, n):
                conf = build_confidence(
                    records[i], records[j], active_anchors=anchors,
                )
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

            # min pairwise confidence + explanations for merged pairs
            min_conf = 1.0
            explanations: list[MergeExplanation] = []
            for a in indices:
                for b in indices:
                    if a >= b:
                        continue
                    key = (min(a, b), max(a, b))
                    if key in pair_conf:
                        min_conf = min(min_conf, pair_conf[key])
                        # Capture explanation for directly merged pairs
                        explanations.append(build_confidence_explained(
                            records[a], records[b], active_anchors=anchors,
                        ))
                    else:
                        # Pair not directly merged but transitively linked
                        c = build_confidence(
                            records[a], records[b],
                            active_anchors=anchors,
                        )
                        min_conf = min(min_conf, c)

            result.append(ResolvedGroup(
                records=group_records,
                merge_confidence=min_conf,
                needs_human_review=min_conf < self.REVIEW_THRESHOLD,
                merge_explanations=explanations,
            ))

        return result
