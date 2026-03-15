"""Coordinate-based PII extraction for fixed-layout documents (Step 21b).

For documents where every page has an identical layout (accounting statements,
payslips, labeled forms), the LLM analyzes the layout ONCE and produces a
``FieldMapping`` list.  This module then uses PyMuPDF word-level bounding boxes
to extract PII from every page in seconds — no LLM calls needed.

Flow:
  1. For each page, get word bounding boxes via ``page.get_text("words")``.
  2. For each ``FieldMapping``, find anchor text on the page.
  3. Compute a search region relative to the anchor based on ``spatial_relationship``.
  4. Collect words in that region and join them.
  5. Apply ``skip_pattern`` and ``value_pattern`` filters.
  6. Map the result to the appropriate ``PIIRecord`` field.

Pages where the PERSON field cannot be extracted are reported as failures
for LLM reconciliation.
"""
from __future__ import annotations

import logging
import re
from uuid import uuid4

import fitz  # PyMuPDF

from app.rra.entity_resolver import PIIRecord
from app.structure.document_schema import FieldMapping

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Field type alias normalization (LLM may return domain-specific names)
# ---------------------------------------------------------------------------

FIELD_TYPE_ALIASES: dict[str, str] = {
    "CLIENT": "PERSON", "CLIENT_NAME": "PERSON",
    "EMPLOYEE": "PERSON", "EMPLOYEE_NAME": "PERSON",
    "MEMBER": "PERSON", "MEMBER_NAME": "PERSON",
    "PATIENT": "PERSON", "PATIENT_NAME": "PERSON",
    "NAME": "PERSON", "FULL_NAME": "PERSON",
    "TAX_NO": "US_SSN", "TAX_NUMBER": "US_SSN",
    "TAX_ID": "US_SSN", "SSN": "US_SSN",
    "NATIONAL_INSURANCE": "NI_NUMBER", "NI_NO": "NI_NUMBER",
    "ADDRESS": "LOCATION", "ADDR": "LOCATION",
    "DOB": "DATE_OF_BIRTH", "BIRTH_DATE": "DATE_OF_BIRTH",
    "PHONE": "PHONE_NUMBER", "TEL": "PHONE_NUMBER",
    "EMAIL": "EMAIL_ADDRESS", "MAIL": "EMAIL_ADDRESS",
    "GOVERNMENT_ID": "US_SSN",
}


def _normalize_field_type(field_type: str) -> str:
    """Normalize domain-specific field types to standard entity types."""
    upper = field_type.upper().strip()
    return FIELD_TYPE_ALIASES.get(upper, upper)


# ---------------------------------------------------------------------------
# Entity type → PIIRecord field mapping (matches llm_template_extractor)
# ---------------------------------------------------------------------------

_FIELD_TO_RAW: dict[str, str] = {
    "PERSON": "raw_name",
    "LOCATION": "raw_address",
    "DATE_OF_BIRTH": "raw_dob",
    "DATE_OF_BIRTH_DMY": "raw_dob",
    "DATE_OF_BIRTH_MDY": "raw_dob",
    "DATE_OF_BIRTH_ISO": "raw_dob",
    "EMAIL_ADDRESS": "raw_email",
    "EMAIL": "raw_email",
    "PHONE_NUMBER": "raw_phone",
    "PHONE_US": "raw_phone",
    "PHONE_INTL": "raw_phone",
    "US_SSN": "raw_government_id",
    "NI_NUMBER": "raw_government_id",
    "AADHAAR": "raw_government_id",
    "US_DRIVER_LICENSE": "raw_government_id",
    "US_PASSPORT": "raw_government_id",
    "PAN_CARD": "raw_government_id",
    "NHS_NUMBER": "raw_government_id",
    "GOVERNMENT_ID": "raw_government_id",
    "IDENTIFICATION_NUMBER": "raw_government_id",
    "NATIONAL_INSURANCE_UK": "raw_government_id",
}

_GOV_ID_TYPES: frozenset[str] = frozenset({
    "US_SSN", "NI_NUMBER", "AADHAAR", "US_DRIVER_LICENSE",
    "US_PASSPORT", "PAN_CARD", "NHS_NUMBER", "GOVERNMENT_ID",
    "IDENTIFICATION_NUMBER", "NATIONAL_INSURANCE_UK",
})

# Tolerance in points for anchor word matching (nearby lines)
_LINE_TOLERANCE = 5


# ---------------------------------------------------------------------------
# CoordinateExtractor
# ---------------------------------------------------------------------------


class CoordinateExtractor:
    """Fast extraction for fixed-layout documents.

    The LLM provides the field map (anchor text + spatial relationships),
    Python extracts from every page using word-level bounding boxes.
    Processes 1000+ pages in seconds.

    Parameters
    ----------
    field_map:
        List of ``FieldMapping`` from the ``DocumentSchema.layout_field_map``.
    doc_path:
        Path to the PDF file.
    doc_id:
        Document ID for ``PIIRecord.source_document_id``.
    """

    def __init__(
        self,
        field_map: list[FieldMapping],
        doc_path: str,
        doc_id: str,
    ) -> None:
        self.field_map = field_map
        self.doc_path = doc_path
        self.doc_id = doc_id

    def extract_all_pages(
        self,
        page_range: list[int] | None = None,
    ) -> tuple[list[PIIRecord], list[int]]:
        """Extract PII from all (or specified) pages.

        Returns
        -------
        tuple[list[PIIRecord], list[int]]
            ``(records, failed_pages)`` — successfully extracted records and
            page numbers (0-based) where extraction failed.
        """
        doc = fitz.open(self.doc_path)
        records: list[PIIRecord] = []
        failed_pages: list[int] = []

        pages_to_process = page_range if page_range is not None else list(range(doc.page_count))

        for page_num in pages_to_process:
            if page_num < 0 or page_num >= doc.page_count:
                failed_pages.append(page_num)
                continue

            page = doc[page_num]
            words = page.get_text("words")
            rotation = page.rotation % 360

            # Collect fields into a dict, then construct frozen PIIRecord
            fields: dict[str, str | dict] = {}
            entity_types_found: list[str] = []
            success = True

            for fm in self.field_map:
                norm_type = _normalize_field_type(fm.field_type)
                value = self._extract_field(words, fm, page, rotation, norm_type)
                if value:
                    raw_field = _FIELD_TO_RAW.get(norm_type)
                    if raw_field:
                        if raw_field == "raw_address":
                            fields[raw_field] = {"full": value}
                        else:
                            fields[raw_field] = value
                        entity_types_found.append(norm_type)
                elif norm_type == "PERSON":
                    # PERSON is mandatory — page fails without it
                    success = False

            raw_name = fields.get("raw_name")
            if success and raw_name and isinstance(raw_name, str):
                rec = PIIRecord(
                    record_id=str(uuid4()),
                    entity_type="PERSON",
                    normalized_value=raw_name,
                    raw_name=raw_name,
                    raw_address=fields.get("raw_address"),
                    raw_phone=fields.get("raw_phone") if isinstance(fields.get("raw_phone"), str) else None,
                    raw_email=fields.get("raw_email") if isinstance(fields.get("raw_email"), str) else None,
                    raw_dob=fields.get("raw_dob") if isinstance(fields.get("raw_dob"), str) else None,
                    raw_government_id=fields.get("raw_government_id") if isinstance(fields.get("raw_government_id"), str) else None,
                    source_document_id=self.doc_id,
                    page_range=str(page_num + 1),
                    entity_types_found=tuple(entity_types_found),
                )
                records.append(rec)
            else:
                failed_pages.append(page_num)

            # Free page memory (PyMuPDF page streaming)
            doc._forget_page(page)

        doc.close()

        logger.info(
            "Coordinate extraction: %d records, %d failed pages (doc=%s)",
            len(records), len(failed_pages), self.doc_id,
        )
        return records, failed_pages

    # -- Field extraction ---------------------------------------------------

    def _extract_field(
        self,
        words: list[tuple],
        field: FieldMapping,
        page: object,
        rotation: int = 0,
        norm_type: str | None = None,
    ) -> str | None:
        """Find anchor text and extract the value at the relative position.

        Parameters
        ----------
        words:
            PyMuPDF word tuples: ``(x0, y0, x1, y1, text, block_no, line_no, word_no)``.
        field:
            The field mapping defining anchor + spatial relationship.
        page:
            PyMuPDF page object (for dimensions).
        rotation:
            Page rotation in degrees (0, 90, 180, 270).
        norm_type:
            Normalized field type (after alias resolution). Falls back to
            ``field.field_type`` if not provided.

        Returns
        -------
        str | None
            Extracted text value, or ``None`` if anchor not found or
            value validation failed.
        """
        anchor_words = self._find_anchor(words, field.anchor_text, rotation)
        if not anchor_words:
            return None

        anchor_bbox = self._merge_bboxes(anchor_words)

        # Define search region based on spatial_relationship + rotation
        region = self._compute_region(anchor_bbox, field, page, rotation)
        if region is None:
            return None

        # Collect words in the region
        region_words_raw = [w for w in words if self._in_region(w, region)]

        # Sort order depends on rotation
        if rotation == 270:
            # Visual top→bottom = increasing y, left→right = decreasing x
            region_words = sorted(region_words_raw, key=lambda w: (w[0], w[1]))
        elif rotation == 90:
            # Visual top→bottom = decreasing y, left→right = increasing x
            region_words = sorted(region_words_raw, key=lambda w: (-w[0], -w[1]))
        elif rotation == 180:
            # Visual left→right = decreasing x, top→bottom = decreasing y
            region_words = sorted(region_words_raw, key=lambda w: (-w[1], -w[0]))
        else:
            # Standard: top→bottom, left→right
            region_words = sorted(region_words_raw, key=lambda w: (w[1], w[0]))

        value = self._words_to_text(region_words, field.line_count, rotation)

        # Apply skip_pattern (remove matching text from value)
        if field.skip_pattern and value:
            try:
                value = re.sub(field.skip_pattern, "", value).strip()
            except re.error:
                pass

        # Skip value_pattern validation for PERSON fields — names are too
        # variable for regex validation (e.g. "(001968) ADELINE CHANDLER").
        effective_type = norm_type or _normalize_field_type(field.field_type)
        if effective_type != "PERSON" and field.value_pattern and value:
            try:
                if not re.search(field.value_pattern, value):
                    return None
            except re.error:
                pass

        return value or None

    # -- Anchor finding -----------------------------------------------------

    @staticmethod
    def _find_anchor(
        words: list[tuple],
        anchor_text: str,
        rotation: int = 0,
    ) -> list[tuple] | None:
        """Find the word(s) matching the anchor text on the page.

        Handles multi-word anchors (e.g., "Tax No") by finding consecutive
        words whose concatenation matches.  For rotated pages (90/270),
        "same line" means similar x values rather than similar y values.

        Returns the matched word tuples, or ``None`` if not found.
        """
        if not anchor_text:
            return None

        anchor_parts = anchor_text.strip().split()
        if not anchor_parts:
            return None

        # For rotated pages, "same line" uses x-axis instead of y-axis
        same_line_idx = 0 if rotation in (90, 270) else 1

        # Single-word anchor
        if len(anchor_parts) == 1:
            target = anchor_parts[0].lower().rstrip(":")
            for w in words:
                word_text = w[4].lower().rstrip(":")
                if word_text == target:
                    return [w]
            return None

        # Multi-word anchor: find consecutive words on the same line
        target_parts = [p.lower().rstrip(":") for p in anchor_parts]
        for i in range(len(words) - len(target_parts) + 1):
            candidate = words[i : i + len(target_parts)]
            # Check all words are on roughly the same line
            if not all(
                abs(candidate[0][same_line_idx] - c[same_line_idx]) < _LINE_TOLERANCE
                for c in candidate
            ):
                continue
            texts = [c[4].lower().rstrip(":") for c in candidate]
            if texts == target_parts:
                return list(candidate)

        return None

    # -- Region computation -------------------------------------------------

    @staticmethod
    def _compute_region(
        anchor_bbox: tuple[float, float, float, float],
        field: FieldMapping,
        page: object,
        rotation: int = 0,
    ) -> tuple[float, float, float, float] | None:
        """Compute the search region based on spatial_relationship + rotation.

        For rotation=0 (standard layout): "right" = +x, "below" = +y.
        For rotation=270: visual "right" = +y at same x band.
        For rotation=90:  visual "right" = -y at same x band.
        For rotation=180: visual "right" = -x, "below" = -y.

        Returns ``(x0, y0, x1, y1)`` or ``None`` if the relationship
        is not recognized.
        """
        ax0, ay0, ax1, ay1 = anchor_bbox
        page_width = page.rect.width
        page_height = page.rect.height

        rel = field.spatial_relationship

        # Parse lines_below_N
        lines_n = None
        if rel.startswith("lines_below_"):
            try:
                lines_n = int(rel.split("_")[-1])
            except ValueError:
                lines_n = 2

        # --- Rotation 0 (standard) ---
        if rotation == 0:
            line_height = (ay1 - ay0) or 15

            if rel == "same_line_right":
                # Limit height to 1.5x anchor line height to avoid
                # capturing words from the next record on the same page.
                max_h = line_height * 1.5
                return (ax1 + 5, ay0 - 5, page_width - 20,
                        min(ay1 + 5, ay0 + max_h))
            if rel == "same_line_left":
                return (20, ay0 - 5, ax0 - 5, ay1 + 5)
            if rel == "line_below":
                return (ax0 - 50, ay1, page_width - 20, ay1 + line_height * 1.8)
            if lines_n is not None:
                return (ax0 - 50, ay1, page_width - 20, ay1 + line_height * lines_n * 1.5)
            if rel == "region_right":
                return (ax1 + 5, ay0 - 5, page_width - 20,
                        ay1 + line_height * max(field.line_count, 1) * 1.5)

        # --- Rotation 270 ---
        # Visual "right" = increasing y at same x band
        elif rotation == 270:
            line_height = (ax1 - ax0) or 15  # "line height" is x-extent

            if rel == "same_line_right":
                return (ax0 - 5, ay1 + 5, ax1 + 5, page_height - 20)
            if rel == "same_line_left":
                return (ax0 - 5, 20, ax1 + 5, ay0 - 5)
            if rel == "line_below":
                return (ax1, ay0 - 50, ax1 + line_height * 1.8, page_height - 20)
            if lines_n is not None:
                return (ax1, ay0 - 50, ax1 + line_height * lines_n * 1.5, page_height - 20)
            if rel == "region_right":
                return (ax0 - 5, ay1 + 5,
                        ax1 + line_height * max(field.line_count, 1) * 1.5,
                        page_height - 20)

        # --- Rotation 90 ---
        # Visual "right" = decreasing y at same x band
        elif rotation == 90:
            line_height = (ax1 - ax0) or 15

            if rel == "same_line_right":
                return (ax0 - 5, 20, ax1 + 5, ay0 - 5)
            if rel == "same_line_left":
                return (ax0 - 5, ay1 + 5, ax1 + 5, page_height - 20)
            if rel == "line_below":
                return (ax0 - line_height * 1.8, ay0 - 50, ax0, page_height - 20)
            if lines_n is not None:
                return (ax0 - line_height * lines_n * 1.5, ay0 - 50, ax0, page_height - 20)
            if rel == "region_right":
                return (ax0 - line_height * max(field.line_count, 1) * 1.5,
                        20, ax1 + 5, ay0 - 5)

        # --- Rotation 180 ---
        # Visual "right" = -x, "below" = -y
        elif rotation == 180:
            line_height = (ay1 - ay0) or 15

            if rel == "same_line_right":
                return (20, ay0 - 5, ax0 - 5, ay1 + 5)
            if rel == "same_line_left":
                return (ax1 + 5, ay0 - 5, page_width - 20, ay1 + 5)
            if rel == "line_below":
                return (20, ay0 - line_height * 1.8, page_width - 20, ay0)
            if lines_n is not None:
                return (20, ay0 - line_height * lines_n * 1.5, page_width - 20, ay0)
            if rel == "region_right":
                return (20, ay0 - line_height * max(field.line_count, 1) * 1.5,
                        ax0 - 5, ay1 + 5)

        # Fallback: treat unknown relationship as same_line_right at rotation 0
        logger.warning(
            "Unknown spatial_relationship %r (rotation=%d), defaulting to same_line_right",
            rel, rotation,
        )
        return (ax1 + 5, ay0 - 5, page_width - 20, ay1 + 5)

    # -- Geometry helpers ---------------------------------------------------

    @staticmethod
    def _merge_bboxes(
        word_tuples: list[tuple],
    ) -> tuple[float, float, float, float]:
        """Merge multiple word bounding boxes into one encompassing bbox."""
        x0 = min(w[0] for w in word_tuples)
        y0 = min(w[1] for w in word_tuples)
        x1 = max(w[2] for w in word_tuples)
        y1 = max(w[3] for w in word_tuples)
        return (x0, y0, x1, y1)

    @staticmethod
    def _in_region(
        word: tuple,
        region: tuple[float, float, float, float],
    ) -> bool:
        """Check if a word's center point falls within the region."""
        wx = (word[0] + word[2]) / 2
        wy = (word[1] + word[3]) / 2
        return (
            region[0] <= wx <= region[2]
            and region[1] <= wy <= region[3]
        )

    @staticmethod
    def _words_to_text(
        region_words: list[tuple],
        line_count: int,
        rotation: int = 0,
    ) -> str:
        """Join words into text, respecting line boundaries.

        Groups words by vertical position (or x-position for rotated pages)
        and joins with spaces (within line) and newlines (between lines).
        Limits output to ``line_count`` lines.
        """
        if not region_words:
            return ""

        # For rotated pages, "same line" = same x band; reading order within
        # a line is by y-position.  For standard pages, same y band, reading
        # order by x-position.
        if rotation in (90, 270):
            group_idx = 0   # group by x
            sort_idx = 1    # sort within line by y
        else:
            group_idx = 1   # group by y
            sort_idx = 0    # sort within line by x

        lines: list[list[tuple]] = []
        current_line: list[tuple] = [region_words[0]]
        for w in region_words[1:]:
            if abs(w[group_idx] - current_line[0][group_idx]) > _LINE_TOLERANCE:
                lines.append(current_line)
                current_line = [w]
            else:
                current_line.append(w)
        lines.append(current_line)

        # Sort words within each line by reading-order axis
        text_lines = []
        for line_words in lines[:line_count]:
            sorted_words = sorted(line_words, key=lambda w: w[sort_idx])
            text_lines.append(" ".join(w[4] for w in sorted_words))

        return "\n".join(text_lines).strip()

