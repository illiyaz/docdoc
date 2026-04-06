"""Schema-based post-filter for Presidio detections (Phase 14b).

``SchemaFilter`` takes a ``DocumentSchema`` (produced by LLM Document
Understanding) and filters/reclassifies Presidio detections.  This is a
pure POST-PROCESSING step — Presidio runs unmodified, then results are
filtered through the schema.

Filtering rules (in order):
1. **Field map matching** — if detected value matches a field_map entry:
   - ``is_pii=False`` → SUPPRESS
   - ``presidio_override`` set → RECLASSIFY
   - detection type in ``suppress_types`` → SUPPRESS
2. **Table-aware filtering** — if detection falls within table region:
   - ``has_pii_columns=False`` → SUPPRESS all detections from table text
   - PII column → KEEP; non-PII column → SUPPRESS
3. **Date context filtering** — if date matches and ``is_pii=False`` → SUPPRESS
4. **People reclassification** — if ORGANIZATION matches a known person → RECLASSIFY to PERSON
5. **Suppression hints** — keyword match → SUPPRESS
6. **No schema match** → KEEP (Presidio detection passes through)

Safety valve: ``schema_confidence < 0.50`` → skip filtering entirely.

Every suppressed/reclassified detection is logged in the suppression log
for audit trail purposes.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.structure.document_schema import DocumentSchema

logger = logging.getLogger(__name__)

# Safety valve: don't filter if LLM confidence is too low
_MIN_SCHEMA_CONFIDENCE = 0.50


@dataclass
class SuppressionEntry:
    """Audit log entry for a suppressed or reclassified detection."""

    entity_type: str
    detected_text: str          # masked for safety — caller should mask before passing
    action: str                 # "suppress" | "reclassify"
    reason: str                 # human-readable reason
    new_entity_type: str | None = None  # set when action="reclassify"


@dataclass
class FilterResult:
    """Result of filtering detections through a DocumentSchema."""

    kept: list                  # detections that passed the filter
    suppressed: list            # detections that were removed
    reclassified: list          # detections that were reclassified (also in kept)
    suppression_log: list[SuppressionEntry] = field(default_factory=list)


class SchemaFilter:
    """Filters Presidio detections through a DocumentSchema to remove false positives.

    Parameters
    ----------
    schema:
        A DocumentSchema produced by LLM Document Understanding.
    """

    def __init__(self, schema: DocumentSchema) -> None:
        self.schema = schema
        self._suppression_log: list[SuppressionEntry] = []
        self._field_value_index: dict[str, list] = {}
        self._table_header_pattern: re.Pattern | None = None
        self._non_pii_table_headers: set[str] = set()
        self._pii_table_columns: dict[str, str] = {}  # header_lower → pii_type

        self._build_field_index()
        self._build_table_index()

    def _build_field_index(self) -> None:
        """Build lookup from normalized value examples to FieldContext entries."""
        for fc in self.schema.field_map:
            key = fc.value_example.strip().lower()
            if key:
                self._field_value_index.setdefault(key, []).append(fc)

    def _build_table_index(self) -> None:
        """Build lookup from table column headers for proximity-based filtering.

        For tables with ``has_pii_columns=False``, all values near known
        non-PII headers are suppressed.  For mixed tables, only non-PII
        column values are suppressed.
        """
        all_non_pii_headers: list[str] = []
        for table in self.schema.tables:
            for col in table.columns:
                header_lower = col.header.strip().lower()
                if not col.contains_pii:
                    all_non_pii_headers.append(re.escape(col.header.strip()))
                    self._non_pii_table_headers.add(header_lower)
                else:
                    if col.pii_type:
                        self._pii_table_columns[header_lower] = col.pii_type

        if all_non_pii_headers:
            pattern = r"\b(?:" + "|".join(all_non_pii_headers) + r")\b"
            self._table_header_pattern = re.compile(pattern, re.IGNORECASE)

    def filter_detections(self, detections: list) -> FilterResult:
        """Filter Presidio detections through the schema.

        Parameters
        ----------
        detections:
            List of DetectionResult objects (from PresidioEngine.analyze()).

        Returns
        -------
        FilterResult
            Contains kept, suppressed, and reclassified lists plus audit log.
        """
        self._suppression_log.clear()

        # Safety valve: low-confidence schema → pass everything through
        if self.schema.schema_confidence < _MIN_SCHEMA_CONFIDENCE:
            logger.debug(
                "Schema confidence %.2f < %.2f; skipping filtering",
                self.schema.schema_confidence, _MIN_SCHEMA_CONFIDENCE,
            )
            return FilterResult(
                kept=list(detections),
                suppressed=[],
                reclassified=[],
                suppression_log=[],
            )

        kept: list = []
        suppressed: list = []
        reclassified: list = []

        for det in detections:
            detected_text = self._get_detected_text(det)
            surrounding = self._get_surrounding_text(det)
            entity_type = det.entity_type

            # --- Rule 1: Field map matching ---
            action = self._check_field_map(detected_text, entity_type, det)
            if action == "suppress":
                suppressed.append(det)
                continue
            if action == "reclassify":
                reclassified.append(det)
                kept.append(det)
                continue

            # --- Rule 2: Table-aware filtering ---
            action = self._check_table(detected_text, entity_type, surrounding, det)
            if action == "suppress":
                suppressed.append(det)
                continue

            # --- Rule 3: Date context filtering ---
            action = self._check_date_context(detected_text, entity_type, det)
            if action == "suppress":
                suppressed.append(det)
                continue

            # --- Rule 4: People reclassification ---
            action = self._check_people(detected_text, entity_type, det)
            if action == "reclassify":
                reclassified.append(det)
                kept.append(det)
                continue

            # --- Rule 5: Suppression hints ---
            action = self._check_suppression_hints(detected_text, entity_type, det)
            if action == "suppress":
                suppressed.append(det)
                continue

            # --- Rule 6: No match → KEEP ---
            kept.append(det)

        return FilterResult(
            kept=kept,
            suppressed=suppressed,
            reclassified=reclassified,
            suppression_log=list(self._suppression_log),
        )

    def get_suppression_log(self) -> list[SuppressionEntry]:
        """Return the audit log of all suppressed/reclassified detections."""
        return list(self._suppression_log)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_detected_text(det) -> str:
        """Extract the detected text span from a DetectionResult."""
        if hasattr(det, "block") and hasattr(det, "start") and hasattr(det, "end"):
            return det.block.text[det.start:det.end]
        return ""

    @staticmethod
    def _get_surrounding_text(det) -> str:
        """Get surrounding context text for a DetectionResult."""
        if hasattr(det, "block") and hasattr(det, "start") and hasattr(det, "end"):
            ctx_start = max(0, det.start - 200)
            ctx_end = min(len(det.block.text), det.end + 200)
            return det.block.text[ctx_start:ctx_end]
        return ""

    def _check_field_map(self, detected_text: str, entity_type: str, det) -> str:
        """Check if detected value matches a field_map entry.

        Returns "suppress", "reclassify", or "" (no action).
        """
        text_lower = detected_text.strip().lower()
        entries = self._field_value_index.get(text_lower, [])

        for fc in entries:
            if not fc.is_pii:
                self._log_suppression(
                    entity_type, detected_text, "suppress",
                    f"field_map: '{fc.label}' → {fc.semantic_type} (not PII)",
                )
                return "suppress"

            if entity_type in fc.suppress_types:
                self._log_suppression(
                    entity_type, detected_text, "suppress",
                    f"field_map: '{fc.label}' suppress_types includes {entity_type}",
                )
                return "suppress"

            if fc.presidio_override and fc.presidio_override != entity_type:
                self._log_suppression(
                    entity_type, detected_text, "reclassify",
                    f"field_map: '{fc.label}' override {entity_type} → {fc.presidio_override}",
                    new_type=fc.presidio_override,
                )
                det.entity_type = fc.presidio_override
                return "reclassify"

        return ""

    def _check_table(
        self, detected_text: str, entity_type: str, surrounding: str, det,
    ) -> str:
        """Check if detection falls within a non-PII table region.

        Strategy 1 (header proximity): if non-PII table headers appear near
        the detection in the flattened text, suppress it.

        Returns "suppress" or "".
        """
        if not self._table_header_pattern or not surrounding:
            return ""

        # Check if any table is fully non-PII and headers appear nearby
        for table in self.schema.tables:
            if table.has_pii_columns:
                # Mixed table: check if detection text looks like it's from a
                # non-PII column (header proximity in surrounding text)
                for col in table.columns:
                    if not col.contains_pii:
                        header_pat = re.compile(
                            re.escape(col.header.strip()) + r"\b", re.IGNORECASE,
                        )
                        if header_pat.search(surrounding):
                            # Non-PII header is nearby — check if a PII header
                            # is also nearby (if so, can't be sure)
                            pii_header_nearby = False
                            for pii_col in table.columns:
                                if pii_col.contains_pii:
                                    pii_pat = re.compile(
                                        re.escape(pii_col.header.strip()) + r"\b",
                                        re.IGNORECASE,
                                    )
                                    if pii_pat.search(surrounding):
                                        pii_header_nearby = True
                                        break
                            if not pii_header_nearby:
                                self._log_suppression(
                                    entity_type, detected_text, "suppress",
                                    f"table: near non-PII column '{col.header}' "
                                    f"({col.semantic_type}) in '{table.table_context}'",
                                )
                                return "suppress"
                continue

            # Fully non-PII table: suppress if any header appears in surrounding text
            if self._table_header_pattern.search(surrounding):
                self._log_suppression(
                    entity_type, detected_text, "suppress",
                    f"table: in non-PII table region '{table.table_context}'",
                )
                return "suppress"

        return ""

    def _check_date_context(self, detected_text: str, entity_type: str, det) -> str:
        """Check if a date detection matches a known non-PII date context.

        Returns "suppress" or "".
        """
        # Only applies to date entity types
        if "DATE" not in entity_type.upper():
            return ""

        text_stripped = detected_text.strip()
        for dc in self.schema.date_contexts:
            if dc.value.strip() == text_stripped and not dc.is_pii:
                self._log_suppression(
                    entity_type, detected_text, "suppress",
                    f"date_context: '{dc.value}' is {dc.semantic_type} (not PII)",
                )
                return "suppress"

        return ""

    def _check_people(self, detected_text: str, entity_type: str, det) -> str:
        """Check if an ORGANIZATION detection matches a known person.

        Returns "reclassify" or "".
        """
        if entity_type != "ORGANIZATION":
            return ""

        text_lower = detected_text.strip().lower()
        for pc in self.schema.people:
            if pc.name.strip().lower() == text_lower:
                self._log_suppression(
                    entity_type, detected_text, "reclassify",
                    f"people: '{pc.name}' is a person ({pc.role}), not an organization",
                    new_type="PERSON",
                )
                det.entity_type = "PERSON"
                return "reclassify"

        return ""

    def _check_suppression_hints(
        self, detected_text: str, entity_type: str, det,
    ) -> str:
        """Check if detected text matches a suppression hint.

        Returns "suppress" or "".
        """
        text_lower = detected_text.strip().lower()
        for hint in self.schema.suppression_hints:
            hint_lower = hint.lower()
            if text_lower in hint_lower or hint_lower in text_lower:
                self._log_suppression(
                    entity_type, detected_text, "suppress",
                    f"suppression_hint: '{hint}'",
                )
                return "suppress"

        return ""

    def _log_suppression(
        self,
        entity_type: str,
        detected_text: str,
        action: str,
        reason: str,
        *,
        new_type: str | None = None,
    ) -> None:
        """Add an entry to the suppression audit log."""
        # Mask the detected text for safety (keep last 4 chars)
        if len(detected_text) > 4:
            masked = "*" * (len(detected_text) - 4) + detected_text[-4:]
        else:
            masked = "*" * len(detected_text)

        entry = SuppressionEntry(
            entity_type=entity_type,
            detected_text=masked,
            action=action,
            reason=reason,
            new_entity_type=new_type,
        )
        self._suppression_log.append(entry)

        logger.debug(
            "SchemaFilter %s: entity_type=%s reason=%s",
            action, entity_type, reason,
        )


# ---------------------------------------------------------------------------
# Frequency-based value suppression (Overnight Pipeline — Phase 3)
# ---------------------------------------------------------------------------
# Detects specific values that appear on >80% of pages, indicating
# organizational metadata (letterhead addresses, company phones, etc.)
# rather than subject PII.

# PII types eligible for value-frequency suppression
_VALUE_FREQ_ELIGIBLE_TYPES = frozenset({
    "PERSON", "LOCATION", "ADDRESS", "PHONE_NUMBER", "EMAIL_ADDRESS",
    "ORGANIZATION", "URL", "FAX_NUMBER",
    # lowercase variants for flexibility
    "person", "location", "address", "phone_number", "email_address",
    "organization", "url", "fax_number",
})

# Threshold: if a specific value appears on more than this fraction
# of pages, flag it as organizational metadata
_VALUE_FREQUENCY_THRESHOLD = 0.80

# Minimum pages before frequency analysis is meaningful
_MIN_PAGES_FOR_FREQUENCY = 3

# Placeholder / blank patterns to always suppress as PERSON
_BLANK_PERSON_PATTERN = re.compile(r"^[\s_\-.*]+$")


@dataclass
class HighFrequencyValue:
    """A PII value detected as organizational metadata due to high page frequency."""

    value: str
    pii_type: str
    page_count: int
    total_pages: int
    frequency: float  # page_count / total_pages

    def to_dict(self) -> dict:
        return {
            "value_masked": self.value[:2] + "***" if len(self.value) > 4 else "***",
            "pii_type": self.pii_type,
            "page_count": self.page_count,
            "total_pages": self.total_pages,
            "frequency": round(self.frequency, 3),
        }


class ValueFrequencyFilter:
    """Detects and suppresses high-frequency PII values within a document.

    Values that appear on >80% of pages are almost certainly organizational
    metadata (letterhead, footer contact info, company name) rather than
    breach-subject PII.

    Usage
    -----
    1. Build the filter from extraction records::

        vff = ValueFrequencyFilter.from_extractions(extractions, total_pages)

    2. Check individual values::

        is_org, reason = vff.is_org_metadata("555-0100", "PHONE_NUMBER")

    3. Filter a list of detections::

        kept, suppressed = vff.filter_detections(detections)
    """

    def __init__(
        self,
        high_freq_values: dict[str, HighFrequencyValue] | None = None,
    ) -> None:
        self._high_freq: dict[str, HighFrequencyValue] = high_freq_values or {}

    @classmethod
    def from_extractions(
        cls,
        extractions: list,
        total_pages: int,
        threshold: float = _VALUE_FREQUENCY_THRESHOLD,
        min_pages: int = _MIN_PAGES_FOR_FREQUENCY,
    ) -> "ValueFrequencyFilter":
        """Build a ValueFrequencyFilter from extraction records.

        Parameters
        ----------
        extractions:
            List of objects with ``pii_type``, ``hashed_value`` or ``detected_text``,
            and ``evidence_page`` attributes.
        total_pages:
            Total pages in the source document.
        threshold:
            Fraction of pages above which a value is considered org metadata.
        min_pages:
            Minimum document page count for frequency analysis to activate.

        Returns
        -------
        ValueFrequencyFilter
        """
        if total_pages < min_pages:
            return cls()

        # Collect pages per (pii_type, value_key)
        value_pages: dict[tuple[str, str], set[int]] = {}
        value_samples: dict[tuple[str, str], str] = {}  # keep one sample for logging

        for ext in extractions:
            pii_type = getattr(ext, "pii_type", None)
            page = getattr(ext, "evidence_page", None)
            if pii_type is None or page is None:
                continue

            # Use hashed_value for dedup key; fall back to detected_text
            val_key = getattr(ext, "hashed_value", None) or ""
            if not val_key:
                val_key = (getattr(ext, "detected_text", None) or "").strip().lower()
            if not val_key:
                continue

            key = (pii_type.upper(), val_key)
            if key not in value_pages:
                value_pages[key] = set()
                # Store a masked sample for audit
                raw = getattr(ext, "detected_text", None) or val_key
                value_samples[key] = raw

            value_pages[key].add(page)

        # Identify high-frequency values
        high_freq: dict[str, HighFrequencyValue] = {}
        for (pii_type, val_key), pages in value_pages.items():
            page_count = len(pages)
            freq = page_count / total_pages
            if freq >= threshold and pii_type.upper() in _VALUE_FREQ_ELIGIBLE_TYPES:
                lookup_key = f"{pii_type}:{val_key}"
                high_freq[lookup_key] = HighFrequencyValue(
                    value=value_samples.get((pii_type, val_key), val_key),
                    pii_type=pii_type,
                    page_count=page_count,
                    total_pages=total_pages,
                    frequency=freq,
                )

        if high_freq:
            logger.info(
                "ValueFrequencyFilter: identified %d high-frequency values "
                "(threshold=%.0f%%, %d pages)",
                len(high_freq), threshold * 100, total_pages,
            )

        return cls(high_freq_values=high_freq)

    def is_org_metadata(self, value: str, pii_type: str) -> tuple[bool, str]:
        """Check if a specific value is flagged as organizational metadata.

        Returns
        -------
        tuple[bool, str]
            (is_org, reason)
        """
        val_key = value.strip().lower()
        lookup = f"{pii_type.upper()}:{val_key}"

        if lookup in self._high_freq:
            hfv = self._high_freq[lookup]
            return True, (
                f"high_frequency_value: appears on {hfv.page_count}/{hfv.total_pages} "
                f"pages ({hfv.frequency:.0%}), likely organizational metadata"
            )

        return False, ""

    @property
    def flagged_values(self) -> list[HighFrequencyValue]:
        """Return all flagged high-frequency values for audit display."""
        return sorted(self._high_freq.values(), key=lambda v: -v.frequency)


def is_blank_or_placeholder(text: str) -> bool:
    """Return True if text is blank, all underscores, or a placeholder pattern.

    These should never be treated as valid PII values.
    """
    if not text or not text.strip():
        return True
    return bool(_BLANK_PERSON_PATTERN.match(text.strip()))


# ---------------------------------------------------------------------------
# Frequency-based analysis (Phase 2 — UI audit improvements)
# ---------------------------------------------------------------------------

# PII types that are commonly organizational metadata when repeated
_ORG_CANDIDATE_TYPES = frozenset({
    "address", "phone", "phone_number", "email", "email_address",
    "organization", "url", "fax", "fax_number",
})

# Threshold: if a value appears on more than this fraction of pages, it's
# likely organizational metadata rather than subject PII
_ORG_FREQUENCY_THRESHOLD = 0.80


@dataclass
class FieldFrequency:
    """Frequency metadata for a single PII type across document pages."""

    pii_type: str
    page_count: int         # number of distinct pages this type appears on
    total_pages: int        # total pages in the document
    is_org_metadata: bool   # True if frequency + type suggest org metadata

    def to_dict(self) -> dict:
        return {
            "pii_type": self.pii_type,
            "page_count": self.page_count,
            "total_pages": self.total_pages,
            "is_org_metadata": self.is_org_metadata,
        }


@dataclass
class PersonFieldContext:
    """Groups PII types by the person they belong to."""

    person_name: str
    role: str               # "primary_subject", "related_party", "institutional"
    pii_types: list[str]

    def to_dict(self) -> dict:
        return {
            "person_name": self.person_name,
            "role": self.role,
            "pii_types": self.pii_types,
        }


def compute_field_frequency(
    extractions: list,
    total_pages: int,
) -> list[FieldFrequency]:
    """Compute per-PII-type page frequency from extraction records.

    Parameters
    ----------
    extractions:
        List of Extraction ORM objects (must have ``pii_type`` and
        ``evidence_page`` attributes).
    total_pages:
        Total number of pages in the source document(s).

    Returns
    -------
    list[FieldFrequency]
        One entry per distinct ``pii_type``, sorted by page_count descending.
    """
    if total_pages < 1:
        total_pages = 1

    # Collect distinct pages per pii_type
    type_pages: dict[str, set[int]] = {}
    for ext in extractions:
        pii_type = getattr(ext, "pii_type", None)
        page = getattr(ext, "evidence_page", None)
        if pii_type is None:
            continue
        pii_lower = pii_type.lower()
        if pii_lower not in type_pages:
            type_pages[pii_lower] = set()
        if page is not None:
            type_pages[pii_lower].add(page)

    results: list[FieldFrequency] = []
    for pii_type, pages in type_pages.items():
        page_count = len(pages) if pages else 1
        ratio = page_count / total_pages
        is_org = (
            ratio >= _ORG_FREQUENCY_THRESHOLD
            and pii_type in _ORG_CANDIDATE_TYPES
        )
        results.append(FieldFrequency(
            pii_type=pii_type,
            page_count=page_count,
            total_pages=total_pages,
            is_org_metadata=is_org,
        ))

    results.sort(key=lambda f: f.page_count, reverse=True)
    return results


def build_person_context(
    extractions: list,
    schema_people: list | None = None,
) -> list[PersonFieldContext]:
    """Group PII types by person using entity_role from extractions.

    Parameters
    ----------
    extractions:
        List of Extraction ORM objects (must have ``pii_type`` and
        ``entity_role`` attributes).
    schema_people:
        Optional list of PersonContext objects from DocumentSchema.

    Returns
    -------
    list[PersonFieldContext]
        One entry per distinct person/role combination.
    """
    # Build role → pii_types mapping from extractions
    role_types: dict[str, set[str]] = {}
    for ext in extractions:
        role = getattr(ext, "entity_role", None) or "unknown"
        pii_type = getattr(ext, "pii_type", None)
        if pii_type is None:
            continue
        if role not in role_types:
            role_types[role] = set()
        role_types[role].add(pii_type.lower())

    # Try to match roles to names from schema people
    role_names: dict[str, str] = {}
    if schema_people:
        for person in schema_people:
            name = getattr(person, "name", "")
            role = getattr(person, "role", "unknown")
            if name and role not in role_names:
                role_names[role] = name

    results: list[PersonFieldContext] = []
    for role, types in role_types.items():
        person_name = role_names.get(role, role.replace("_", " ").title())
        results.append(PersonFieldContext(
            person_name=person_name,
            role=role,
            pii_types=sorted(types),
        ))

    # Sort: primary_subject first, then alphabetically
    def sort_key(pc: PersonFieldContext) -> tuple:
        if pc.role == "primary_subject":
            return (0, pc.person_name)
        if pc.role == "related_party":
            return (1, pc.person_name)
        return (2, pc.person_name)

    results.sort(key=sort_key)
    return results
