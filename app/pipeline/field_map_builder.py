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
_LINE_TOLERANCE = 8


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

        # Deduplicate: keep first entry per field type, drop empty anchors
        seen_types: set[str] = set()
        deduped: list[FieldMapping] = []
        for fm in field_maps:
            norm_type = fm.field_type.upper()
            if not fm.anchor_text or not fm.anchor_text.strip():
                logger.debug("Dropping field with empty anchor: %s", fm.field_type)
                continue
            if norm_type in seen_types:
                logger.debug("Dropping duplicate field type: %s", fm.field_type)
                continue
            seen_types.add(norm_type)
            deduped.append(fm)

        return deduped

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

        # Step 2: find the value NEAR the label (prefer closest match)
        label_bbox = _merge_bboxes(label_words)
        value_words = self._find_text_in_words(words, value_text, near_label=label_bbox)

        if not value_words:
            # Fuzzy fallback: search for first word of value
            first_word = value_text.split()[0] if value_text.strip() else ""
            if first_word and len(first_word) >= 3:
                value_words = self._find_text_in_words(words, first_word, near_label=label_bbox)

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
        near_label: tuple[float, float, float, float] | None = None,
    ) -> list[tuple] | None:
        """Find *text* in PyMuPDF word list.

        Handles:
        - Exact single-word match
        - Multi-word phrases (consecutive words on same line)
        - Case-insensitive matching
        - Partial matching (first N words of a long value)
        - Compound PyMuPDF words (e.g. ``"STREET,11TH"`` matching ``"STREET"``)

        If *near_label* is provided (label bbox), prefer the match closest
        to the label when multiple matches exist.
        """
        if not text or not words:
            return None

        text_parts = text.strip().split()
        if not text_parts:
            return None

        all_matches: list[list[tuple]] = []

        # --- single-word ---
        if len(text_parts) == 1:
            target = text_parts[0].lower().rstrip(":.,;")
            for w in words:
                word_text = w[4].lower().rstrip(":.,;")
                if word_text == target:
                    all_matches.append([w])
                elif target in word_text and len(target) >= 3:
                    all_matches.append([w])
            if not all_matches:
                return None
            return _pick_nearest(all_matches, near_label)

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
                    all_matches.append(result)
            # If we found matches at this match_len, pick the best and stop
            if all_matches:
                return _pick_nearest(all_matches, near_label)

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

        Uses the FIRST (topmost) value word for y-comparison so that
        multi-line values don't skew the spatial relationship.

        Returns ``(relationship_string, line_count)``.
        """
        label_bbox = _merge_bboxes(label_words)
        value_bbox = _merge_bboxes(value_words)

        label_cy = (label_bbox[1] + label_bbox[3]) / 2

        # Use the topmost value word for y-comparison (not merged bbox center).
        # This prevents multi-line values from being classified as "lines_below"
        # when the first line is actually on the same line as the label.
        first_value_word = min(value_words, key=lambda w: w[1])
        value_first_cy = (first_value_word[1] + first_value_word[3]) / 2

        line_height = (label_bbox[3] - label_bbox[1]) or 15
        if line_height < 5:
            line_height = 15  # fallback for tiny labels

        y_diff = value_first_cy - label_cy

        logger.debug(
            "Spatial: label bbox=%s (cy=%.1f), first value word='%s' at y=(%.1f,%.1f) cy=%.1f, y_diff=%.1f",
            label_bbox, label_cy,
            first_value_word[4], first_value_word[1], first_value_word[3],
            value_first_cy, y_diff,
        )

        # Same line
        if abs(y_diff) <= _LINE_TOLERANCE:
            if value_bbox[0] >= label_bbox[0]:
                return ("same_line_right", 1)
            return ("same_line_right", 1)  # fallback for overlapping

        # Below
        if y_diff > 0:
            lines_below = max(1, round(y_diff / line_height))
            if lines_below == 1:
                return ("line_below", 1)
            value_line_count = _count_value_lines(value_words)
            return (f"lines_below_{lines_below}", max(value_line_count, 1))

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


def _pick_nearest(
    matches: list[list[tuple]],
    near_label: tuple[float, float, float, float] | None,
) -> list[tuple]:
    """Pick the match closest to *near_label*, or the first match if no label."""
    if len(matches) == 1 or near_label is None:
        return matches[0]

    label_cx = (near_label[0] + near_label[2]) / 2
    label_cy = (near_label[1] + near_label[3]) / 2

    def _distance(match: list[tuple]) -> float:
        bbox = _merge_bboxes(match)
        mx = (bbox[0] + bbox[2]) / 2
        my = (bbox[1] + bbox[3]) / 2
        return ((mx - label_cx) ** 2 + (my - label_cy) ** 2) ** 0.5

    return min(matches, key=_distance)


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