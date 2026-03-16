"""Build FieldMapping from vision-identified PII + PyMuPDF word coordinates (Step 22b).

Vision tells us WHAT exists. PyMuPDF tells us WHERE it is. This module bridges
the two to produce deterministic FieldMappings for CoordinateExtractor.

Flow:
  1. VisionRouter says: "I see PERSON='ADELINE CHANDLER' near label 'Client:'"
  2. FieldMapBuilder searches PyMuPDF words for "ADELINE" and "Client:"
  3. Computes: anchor_text="Client:", spatial_relationship="same_line_right"
  4. Returns a validated FieldMapping ready for CoordinateExtractor
"""
from __future__ import annotations

import logging
import re

import fitz  # PyMuPDF

from app.pipeline.vision_router import VisionRoutingResult
from app.structure.document_schema import FieldMapping

logger = logging.getLogger(__name__)

# Vertical tolerance in points for same-line detection
_LINE_TOLERANCE = 5


class FieldMapBuilder:
    """Build coordinate-based FieldMappings from vision PII + PyMuPDF words."""

    def build_field_map(
        self,
        vision_result: VisionRoutingResult,
        doc_path: str,
        page_num: int = 0,
    ) -> list[FieldMapping]:
        """Build FieldMapping list from vision-identified PII fields.

        For each PII field from vision:
        1. Find the label text (e.g., "Client:") in PyMuPDF words
        2. Find the value text (e.g., "ADELINE CHANDLER") in PyMuPDF words
        3. Compute spatial relationship between label and value
        4. Build a FieldMapping with anchor, relationship, and pattern

        Returns list of validated FieldMappings.
        """
        doc = fitz.open(doc_path)
        try:
            if page_num >= len(doc):
                logger.warning(
                    "Page %d out of range (doc has %d pages)", page_num, len(doc),
                )
                return []
            page = doc[page_num]
            words = page.get_text("words")
        finally:
            doc.close()

        # PyMuPDF words: (x0, y0, x1, y1, text, block_no, line_no, word_no)
        field_maps: list[FieldMapping] = []
        for pii_field in vision_result.pii_fields:
            fm = self._build_one_field(pii_field, words)
            if fm is not None:
                field_maps.append(fm)

        return field_maps

    # ------------------------------------------------------------------
    # Per-field builder
    # ------------------------------------------------------------------

    def _build_one_field(
        self,
        pii_field: dict,
        words: list[tuple],
    ) -> FieldMapping | None:
        """Build a FieldMapping for one PII field.

        ``pii_field`` has keys: type, value, label, position (from vision).
        ``words``: PyMuPDF word tuples from the page.
        """
        field_type = pii_field.get("type", "")
        value_text = pii_field.get("value", "")
        label_text = pii_field.get("label", "")

        if not field_type or not value_text:
            return None

        # Step 1: find the label in PyMuPDF words
        label_words = self._find_text_in_words(words, label_text) if label_text else None

        # No label → skip (coordinate extraction needs an anchor)
        if not label_words:
            return None

        # Step 2: find the value in PyMuPDF words
        value_words = self._find_text_in_words(words, value_text)

        if not value_words:
            # Fuzzy fallback: search for first word of value
            first_word = value_text.split()[0] if value_text.strip() else ""
            if first_word and len(first_word) >= 3:
                value_words = self._find_text_in_words(words, first_word)

        if not value_words:
            logger.warning("Could not find '%s' in PyMuPDF words", value_text[:30])
            return None

        # Step 3: compute spatial relationship
        spatial_rel, line_count = self._compute_spatial_relationship(
            label_words, value_words,
        )

        # Clean anchor text (strip trailing punctuation)
        anchor_text = label_text.rstrip(":.,; ")

        # Step 4: value pattern (for validation)
        value_pattern = self._infer_value_pattern(field_type, value_text)

        # Step 5: skip pattern (noise between label and value)
        skip_pattern = self._infer_skip_pattern(label_words, value_words, words)

        # Step 6: bounding box of the sample value
        value_bbox = _merge_bboxes(value_words)

        return FieldMapping(
            field_type=field_type,
            anchor_text=anchor_text,
            spatial_relationship=spatial_rel,
            value_pattern=value_pattern,
            sample_bbox=list(value_bbox),
            line_count=line_count,
            skip_pattern=skip_pattern,
        )

    # ------------------------------------------------------------------
    # Word-level text search
    # ------------------------------------------------------------------

    @staticmethod
    def _find_text_in_words(
        words: list[tuple],
        text: str,
    ) -> list[tuple] | None:
        """Find *text* in PyMuPDF word list.

        Handles:
        - Exact single-word match
        - Multi-word phrases (consecutive words on same line)
        - Case-insensitive matching
        - Partial matching (first N words of a long value)
        - Compound PyMuPDF words (e.g. ``"STREET,11TH"`` matching ``"STREET"``)
        """
        if not text or not words:
            return None

        text_parts = text.strip().split()
        if not text_parts:
            return None

        # --- single-word ---
        if len(text_parts) == 1:
            target = text_parts[0].lower().rstrip(":.,;")
            for w in words:
                word_text = w[4].lower().rstrip(":.,;")
                if word_text == target:
                    return [w]
                # Substring match for compound words (e.g. "STREET,11TH")
                if target in word_text and len(target) >= 3:
                    return [w]
            return None

        # --- multi-word: find consecutive words on the same line ---
        target_parts = [p.lower().rstrip(":.,;") for p in text_parts]

        for match_len in [
            min(3, len(target_parts)),
            min(2, len(target_parts)),
            1,
        ]:
            target_subset = target_parts[:match_len]
            for i in range(len(words) - match_len + 1):
                candidate = words[i : i + match_len]
                # All on the same line?
                if not all(
                    abs(candidate[0][1] - c[1]) < _LINE_TOLERANCE for c in candidate
                ):
                    continue
                texts = [c[4].lower().rstrip(":.,;") for c in candidate]
                if texts == target_subset:
                    # Extend to include remaining target words on same line
                    result = list(candidate)
                    for j in range(
                        i + match_len, min(i + len(target_parts), len(words))
                    ):
                        if abs(words[j][1] - candidate[0][1]) < _LINE_TOLERANCE:
                            result.append(words[j])
                        else:
                            break
                    return result

        return None

    # ------------------------------------------------------------------
    # Spatial relationship
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_spatial_relationship(
        label_words: list[tuple],
        value_words: list[tuple],
    ) -> tuple[str, int]:
        """Compute spatial relationship between label and value.

        Returns ``(relationship_string, line_count)``.
        """
        label_bbox = _merge_bboxes(label_words)
        value_bbox = _merge_bboxes(value_words)

        label_cy = (label_bbox[1] + label_bbox[3]) / 2
        value_cy = (value_bbox[1] + value_bbox[3]) / 2

        line_height = (label_bbox[3] - label_bbox[1]) or 15

        y_diff = value_cy - label_cy

        # Same line
        if abs(y_diff) < _LINE_TOLERANCE:
            if value_bbox[0] > label_bbox[2]:
                return ("same_line_right", 1)
            return ("same_line_right", 1)  # fallback for overlapping

        # Below
        lines_below = round(y_diff / line_height)
        if lines_below == 1:
            return ("line_below", 1)
        if lines_below > 1:
            value_line_count = _count_value_lines(value_words)
            return (f"lines_below_{lines_below}", max(value_line_count, lines_below))

        # Above (unusual) — fallback
        return ("same_line_right", 1)

    # ------------------------------------------------------------------
    # Value pattern inference
    # ------------------------------------------------------------------

    @staticmethod
    def _infer_value_pattern(field_type: str, value_text: str) -> str | None:
        """Infer a regex validation pattern from field type."""
        patterns: dict[str, str] = {
            "US_SSN": r"\d{3}-\d{2}-\d{4}",
            "GOVERNMENT_ID": r"\d{3}-\d{2}-\d{4}|\d{2}-\d{7}",
            "PHONE_NUMBER": r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}",
            "EMAIL_ADDRESS": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
            "DATE_OF_BIRTH": r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}",
            "NI_NUMBER": r"[A-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-Z]",
        }
        return patterns.get(field_type)

    # ------------------------------------------------------------------
    # Skip pattern detection
    # ------------------------------------------------------------------

    @staticmethod
    def _infer_skip_pattern(
        label_words: list[tuple],
        value_words: list[tuple],
        all_words: list[tuple],
    ) -> str | None:
        """Detect noise between label and value that should be skipped.

        Example: ``"Client: (001968) ADELINE CHANDLER"``
        The ``"(001968)"`` is noise → skip pattern ``r"\\(\\d+\\)\\s*"``.
        """
        label_bbox = _merge_bboxes(label_words)
        value_bbox = _merge_bboxes(value_words)

        # Only detect between-text on the same line
        between_words = []
        for w in all_words:
            if (
                abs(w[1] - label_bbox[1]) < _LINE_TOLERANCE
                and w[0] > label_bbox[2]
                and w[0] < value_bbox[0]
            ):
                between_words.append(w)

        if not between_words:
            return None

        between_text = " ".join(
            w[4] for w in sorted(between_words, key=lambda w: w[0])
        )

        # Common noise patterns
        if re.match(r"^\(\d+\)\s*$", between_text):
            return r"\(\d+\)\s*"
        if re.match(r"^:\s*$", between_text):
            return r"[:.]\s*"

        return None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _merge_bboxes(word_tuples: list[tuple]) -> tuple[float, float, float, float]:
    """Merge multiple word bounding boxes into one encompassing bbox."""
    x0 = min(w[0] for w in word_tuples)
    y0 = min(w[1] for w in word_tuples)
    x1 = max(w[2] for w in word_tuples)
    y1 = max(w[3] for w in word_tuples)
    return (x0, y0, x1, y1)


def _count_value_lines(value_words: list[tuple]) -> int:
    """Count how many text lines the value spans."""
    if not value_words:
        return 1
    lines: set[int] = set()
    for w in value_words:
        y = round(w[1] / _LINE_TOLERANCE) * _LINE_TOLERANCE
        lines.add(y)
    return len(lines)
