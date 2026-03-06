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
