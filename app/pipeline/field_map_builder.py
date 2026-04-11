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


def _derotate_words(
    words: list[tuple],
    rotation: int,
    page_width: float,
    page_height: float,
) -> list[tuple]:
    """Transform word coordinates from raw PDF space to visual (rotation=0) space.

    PyMuPDF sometimes returns raw coordinates without applying the page /Rotate
    transform.  This function maps them to a standard coordinate system where
    x increases left-to-right and y increases top-to-bottom.

    For rotation=0 the words are returned unchanged.
    """
    if rotation == 0:
        return words

    derotated: list[tuple] = []
    for w in words:
        x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
        rest = w[5:]

        if rotation == 270:
            # Visual: left→right = +y, top→bottom = -x (high x = top)
            nx0 = y0
            ny0 = page_width - x1
            nx1 = y1
            ny1 = page_width - x0
        elif rotation == 90:
            # Visual: left→right = -y, top→bottom = +x
            nx0 = page_height - y1
            ny0 = x0
            nx1 = page_height - y0
            ny1 = x1
        elif rotation == 180:
            # Visual: left→right = -x, top→bottom = -y
            nx0 = page_width - x1
            ny0 = page_height - y1
            nx1 = page_width - x0
            ny1 = page_height - y0
        else:
            nx0, ny0, nx1, ny1 = x0, y0, x1, y1

        derotated.append((nx0, ny0, nx1, ny1, text) + rest)

    return derotated


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
            raw_words = page.get_text("words")
            rotation = page.rotation
            page_w = page.rect.width
            page_h = page.rect.height
        finally:
            doc.close()

        # Derotate word coordinates so all spatial logic uses standard axes
        # (x = left→right, y = top→bottom).  PyMuPDF does not always derotate.
        words = _derotate_words(raw_words, rotation, page_w, page_h)
        if rotation:
            logger.info(
                "FieldMapBuilder: page %d has rotation=%d, derotated %d words",
                page_num, rotation, len(words),
            )

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

        # Treat "discovered" label (from person_discovery fallback) as empty
        if label_text in ("discovered", "Discovered"):
            label_text = ""

        # Step 1: find the label in PyMuPDF words
        label_words = self._find_text_in_words(words, label_text) if label_text else None

        # No label found → for PERSON, try to find nearby static text as anchor
        if not label_words and field_type.upper() == "PERSON":
            label_words, label_text = self._find_nearby_anchor_for_value(
                words, value_text,
            )

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

        # Step 7: entity_role from vision pii_field or segregation data
        entity_role = pii_field.get("role")

        return FieldMapping(
            field_type=field_type,
            anchor_text=anchor_text,
            spatial_relationship=spatial_rel,
            value_pattern=value_pattern,
            sample_bbox=list(value_bbox),
            line_count=line_count,
            skip_pattern=skip_pattern,
            entity_role=entity_role,
        )

    # ------------------------------------------------------------------
    # Anchor discovery for unlabeled PERSON fields
    # ------------------------------------------------------------------

    def _find_nearby_anchor_for_value(
        self,
        words: list[tuple],
        value_text: str,
    ) -> tuple[list[tuple] | None, str]:
        """Find nearby static text that can serve as an anchor for an unlabeled field.

        When a PERSON field has no label (e.g., name appears without "Name:" prefix),
        find the value in PyMuPDF words, then look for text to the LEFT on the same
        line, or on the line ABOVE. That nearby text becomes the anchor.

        Returns (anchor_words, anchor_text) or (None, "") if no anchor found.
        """
        # First, find the value itself
        value_words = self._find_text_in_words(words, value_text)
        if not value_words:
            first_word = value_text.split()[0] if value_text.strip() else ""
            if first_word and len(first_word) >= 3:
                value_words = self._find_text_in_words(words, first_word)
        if not value_words:
            return None, ""

        value_bbox = _merge_bboxes(value_words)
        value_y = (value_bbox[1] + value_bbox[3]) / 2
        value_x0 = value_bbox[0]

        # Look for words to the LEFT on the same line (within LINE_TOLERANCE)
        same_line_left: list[tuple] = []
        for w in words:
            w_y = (w[1] + w[3]) / 2
            if abs(w_y - value_y) < _LINE_TOLERANCE and w[2] < value_x0 - 5:
                same_line_left.append(w)

        if same_line_left:
            same_line_left.sort(key=lambda w: -w[2])  # rightmost first
            anchor_words = same_line_left[:3]
            anchor_words.sort(key=lambda w: w[0])  # restore left-to-right
            anchor_text = " ".join(w[4].strip() for w in anchor_words).strip()
            if anchor_text and len(anchor_text) >= 2:
                logger.info(
                    "Found nearby anchor '%s' for unlabeled PERSON '%s'",
                    anchor_text[:30], value_text[:30],
                )
                return anchor_words, anchor_text

        # Look for text on the line ABOVE (within 30pt vertical distance)
        above_words: list[tuple] = []
        for w in words:
            w_y = (w[1] + w[3]) / 2
            if value_y - 30 < w_y < value_y - 3:
                if abs(w[0] - value_x0) < 100:
                    above_words.append(w)

        if above_words:
            above_words.sort(key=lambda w: (w[1], w[0]))
            anchor_words = above_words[:3]
            anchor_text = " ".join(w[4].strip() for w in anchor_words).strip()
            if anchor_text and len(anchor_text) >= 2:
                logger.info(
                    "Found above-line anchor '%s' for unlabeled PERSON '%s'",
                    anchor_text[:30], value_text[:30],
                )
                return anchor_words, anchor_text

        return None, ""

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


# ──────────────────────────────────────────────────────────────
# Auto-correct LLM-produced field maps using real word positions
# ──────────────────────────────────────────────────────────────

def auto_correct_field_map(
    field_maps: list[FieldMapping],
    doc_path: str,
    onset_page: int = 0,
) -> list[FieldMapping]:
    """Correct LLM-produced spatial relationships using actual PyMuPDF words.

    The LLM understands WHAT fields exist and WHAT anchor to use, but
    guesses the spatial distance (e.g., "line_below" when the real gap
    is 7 lines).  This function:

    1. Opens the PDF at the onset page
    2. Finds each anchor in the actual word list
    3. Finds content lines below the anchor
    4. Recomputes the real spatial relationship
    5. Adds accurate sample_bbox from the actual value position

    Returns corrected FieldMappings.  Mappings whose anchor can't be
    found are returned unchanged (coordinate extractor will handle
    the miss gracefully on a per-page basis).
    """
    if not field_maps or not doc_path:
        return field_maps

    try:
        doc = fitz.open(doc_path)
    except Exception:
        return field_maps

    try:
        if onset_page >= len(doc):
            return field_maps
        page = doc[onset_page]
        raw_words = page.get_text("words")
        rotation = page.rotation
        page_w = page.rect.width
        page_h = page.rect.height
    finally:
        doc.close()

    words = _derotate_words(raw_words, rotation, page_w, page_h)
    if not words:
        return field_maps

    # Build a sorted list of distinct text lines (by y-center)
    line_ys: list[float] = []
    seen_y: set[int] = set()
    for w in sorted(words, key=lambda w: w[1]):
        y_bucket = round((w[1] + w[3]) / 2 / _LINE_TOLERANCE) * _LINE_TOLERANCE
        if y_bucket not in seen_y:
            seen_y.add(y_bucket)
            line_ys.append((w[1] + w[3]) / 2)

    corrected: list[FieldMapping] = []
    for fm in field_maps:
        corrected_fm = _correct_one_field(fm, words, line_ys)
        corrected.append(corrected_fm)

    return corrected


def _find_anchor_words(anchor: str, words: list[tuple]) -> list[tuple] | None:
    """Find anchor text in PyMuPDF words, handling masked placeholders."""
    anchor_words = FieldMapBuilder._find_text_in_words(words, anchor)
    if anchor_words:
        return anchor_words

    # Try partial match (anchor might be masked: [PHONE] vs 425-431-6400)
    anchor_lower = anchor.lower().strip("[]")
    for w in words:
        if anchor_lower in w[4].lower() or w[4].lower() in anchor_lower:
            return [w]

    # Try matching the unmasked phone pattern
    if anchor.startswith("[") and "PHONE" in anchor.upper():
        import re as _re
        for w in words:
            if _re.match(r'\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', w[4]):
                return [w]

    return None


def _find_data_lines_after_gap(
    anchor_cy: float,
    line_ys: list[float],
    anchor_line_height: float,
) -> list[float]:
    """Find content lines below the anchor, treating the first cluster as data.

    Many documents have a large blank gap between the header/anchor area
    and the data section (e.g., school header at y=100, student data at
    y=180).  The LLM's "line_below" means "first line in the data section",
    not literally the next text line from the anchor.

    Strategy: take all content lines below the anchor.  The first content
    line IS the start of the data section (even if there's a gap from the
    anchor).  The gap from anchor to first content is just a layout gap —
    the coordinate extractor handles it via the corrected lines_below_N.

    We do NOT skip past the first content cluster because the data we want
    (names, addresses) is IN that first cluster, not after it.
    """
    all_below: list[float] = [
        ly for ly in line_ys if ly > anchor_cy + _LINE_TOLERANCE
    ]
    return all_below


def _correct_one_field(
    fm: FieldMapping,
    words: list[tuple],
    line_ys: list[float],
) -> FieldMapping:
    """Correct one FieldMapping's spatial relationship using real word positions.

    Handles blank gaps between anchor and data section by collapsing
    them — the LLM's "line_below" refers to the Nth content line in
    the data section, not the Nth text line from the anchor.
    """
    anchor = fm.anchor_text
    if not anchor:
        return fm

    anchor_words = _find_anchor_words(anchor, words)
    if not anchor_words:
        logger.debug("auto_correct: anchor '%s' not found on page", anchor[:30])
        return fm

    anchor_bbox = _merge_bboxes(anchor_words)
    anchor_cy = (anchor_bbox[1] + anchor_bbox[3]) / 2
    anchor_line_height = (anchor_bbox[3] - anchor_bbox[1]) or 12
    if anchor_line_height < 8:
        anchor_line_height = 14  # reasonable default

    # Same-line relationships don't need correction
    rel = fm.spatial_relationship or "line_below"
    if rel == "same_line_right":
        return fm

    # Find data lines after any blank gap
    data_lines = _find_data_lines_after_gap(anchor_cy, line_ys, anchor_line_height)
    if not data_lines:
        return fm

    # Parse which content line the LLM intended (line_below=1, lines_below_3=3)
    intended_line = 1
    if rel.startswith("lines_below_"):
        try:
            intended_line = int(rel.split("_")[-1])
        except ValueError:
            intended_line = 2

    # Map LLM's intended Nth content line to actual position
    target_idx = min(intended_line - 1, len(data_lines) - 1)
    actual_y = data_lines[target_idx]

    # Compute the real spatial relationship (from anchor, in line_height units)
    real_y_diff = actual_y - anchor_cy
    real_lines = max(1, round(real_y_diff / anchor_line_height))

    if real_lines == 1:
        new_rel = "line_below"
    else:
        new_rel = f"lines_below_{real_lines}"

    # Find words at the target line to get accurate bbox
    target_words = [
        w for w in words
        if abs((w[1] + w[3]) / 2 - actual_y) < _LINE_TOLERANCE
    ]
    new_bbox = list(_merge_bboxes(target_words)) if target_words else fm.sample_bbox

    # Count value lines (for multi-line fields like addresses)
    value_line_count = fm.line_count
    if value_line_count > 1 and target_idx + value_line_count <= len(data_lines):
        last_y = data_lines[target_idx + value_line_count - 1]
        real_span = max(1, round((last_y - actual_y) / anchor_line_height)) + 1
        value_line_count = real_span

    if new_rel != fm.spatial_relationship:
        logger.info(
            "auto_correct: '%s' %s → %s (anchor '%s' at y=%.0f, data at y=%.0f, gap=%.0fpt)",
            fm.field_type, fm.spatial_relationship, new_rel,
            anchor[:20], anchor_cy, actual_y, real_y_diff,
        )

    return FieldMapping(
        field_type=fm.field_type,
        anchor_text=fm.anchor_text,
        spatial_relationship=new_rel,
        value_pattern=fm.value_pattern,
        sample_bbox=new_bbox,
        line_count=value_line_count,
        skip_pattern=fm.skip_pattern,
        entity_role=fm.entity_role,
    )