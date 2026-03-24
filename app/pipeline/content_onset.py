"""Generalized content onset detection for all file types.

PDF: delegates to find_data_onset() from app/readers/onset.py.
CSV/Excel/Parquet (tabular): onset is always page/sheet 0.
DOCX/HTML/EML (prose): scan blocks for ONSET_SIGNALS patterns.

PII-Verified Onset (Step 13-onset):
  Two-pass approach for finding the TRUE first page where PII exists:
  Pass 1 (Heuristic): scan for ONSET_SIGNALS text patterns → candidate pages
  Pass 2 (Presidio): run PII detection on candidates to verify actual PII presence
"""
from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING

from app.readers.base import ExtractedBlock
from app.readers.onset import _COMPILED_SIGNALS

if TYPE_CHECKING:
    from app.pii.presidio_engine import PresidioEngine

logger = logging.getLogger(__name__)

_TABULAR = frozenset({"csv", "xlsx", "xls", "parquet", "avro"})


def compute_sample_pages(total_pages: int) -> list[int]:
    """Return sorted 0-based page numbers to sample for structure analysis.

    Tiered strategy:
      1-10 pages   → all pages
      11-50        → first 5 + last 2 + 3 random middle
      51-200       → first 5 + last 2 + 8 random middle
      201-500      → first 7 + last 3 + 10 random middle
      500+         → first 8 + last 3 + 14 random middle

    Uses a deterministic seed (total_pages) for reproducibility.
    """
    if total_pages <= 0:
        return []

    if total_pages <= 10:
        return list(range(total_pages))

    if total_pages <= 50:
        first_n, last_n, mid_n = 5, 2, 3
    elif total_pages <= 200:
        first_n, last_n, mid_n = 5, 2, 8
    elif total_pages <= 500:
        first_n, last_n, mid_n = 7, 3, 10
    else:
        first_n, last_n, mid_n = 8, 3, 14

    first_pages = set(range(first_n))
    last_pages = set(range(total_pages - last_n, total_pages))

    # Middle pages: everything not in first or last
    middle_pool = [p for p in range(total_pages) if p not in first_pages and p not in last_pages]
    rng = random.Random(total_pages)  # deterministic seed
    mid_sample = set(rng.sample(middle_pool, min(mid_n, len(middle_pool))))

    return sorted(first_pages | last_pages | mid_sample)
_PII_VERIFICATION_SCORE = 0.70  # minimum confidence to count as verified PII
_MAX_SEQUENTIAL_SCAN = 20  # max pages to scan if no heuristic candidates have PII
_MAX_HEURISTIC_CANDIDATES = 5  # max candidate pages from heuristic pass


def find_content_onset_from_blocks(
    blocks: list[ExtractedBlock],
    file_type: str,
) -> int | str:
    """Return the page_or_sheet value where meaningful content begins.

    For tabular formats, returns 0 (data starts immediately).
    For prose formats, scans blocks for onset signal patterns and returns
    the page_or_sheet of the first matching block, or 0 if none found.
    """
    if file_type.lower() in _TABULAR:
        return 0

    for block in blocks:
        if any(p.search(block.text) for p in _COMPILED_SIGNALS):
            return block.page_or_sheet

    return 0


def filter_sample_blocks(
    blocks: list[ExtractedBlock],
    onset_page: int | str,
    file_type: str,
    *,
    max_tabular_rows: int = 50,
    max_prose_blocks: int = 20,
    pdf_extra_pages: int = 2,
) -> list[ExtractedBlock]:
    """Filter blocks to only the sample set from the onset page/sheet.

    PDF: all blocks on the onset page + next ``pdf_extra_pages`` pages.
         Many documents have summary/financial info on the onset page with
         personal details (name, address, DOB) on the following page(s).
    Tabular: first max_tabular_rows blocks from onset sheet.
    Prose: first max_prose_blocks blocks starting from onset page.
    """
    if file_type.lower() == "pdf":
        # Collect distinct page numbers in order
        all_pages: list[int | str] = []
        seen: set[int | str] = set()
        for b in blocks:
            if b.page_or_sheet not in seen:
                seen.add(b.page_or_sheet)
                all_pages.append(b.page_or_sheet)

        # Find onset page index, include onset + next N pages
        try:
            idx = all_pages.index(onset_page)
        except ValueError:
            idx = 0
        sample_pages = set(all_pages[idx : idx + 1 + pdf_extra_pages])
        return [b for b in blocks if b.page_or_sheet in sample_pages]

    if file_type.lower() in _TABULAR:
        result = []
        for b in blocks:
            if b.page_or_sheet == onset_page:
                result.append(b)
                if len(result) >= max_tabular_rows:
                    break
        return result

    # Prose: take first N blocks from onset page onward
    result = []
    found_onset = False
    for b in blocks:
        if b.page_or_sheet == onset_page:
            found_onset = True
        if found_onset:
            result.append(b)
            if len(result) >= max_prose_blocks:
                break
    return result


# ---------------------------------------------------------------------------
# PII-Verified Onset Detection (Step 13-onset)
# ---------------------------------------------------------------------------


def _get_heuristic_candidate_pages(
    blocks: list[ExtractedBlock],
) -> list[int | str]:
    """Pass 1: scan blocks for ONSET_SIGNALS and return up to 5 candidate pages.

    Returns distinct page_or_sheet values where at least one onset signal matched,
    in the order they appear. Capped at _MAX_HEURISTIC_CANDIDATES.
    """
    seen: set[int | str] = set()
    candidates: list[int | str] = []
    for block in blocks:
        if block.page_or_sheet in seen:
            continue
        if any(p.search(block.text) for p in _COMPILED_SIGNALS):
            seen.add(block.page_or_sheet)
            candidates.append(block.page_or_sheet)
            if len(candidates) >= _MAX_HEURISTIC_CANDIDATES:
                break
    return candidates


def _verify_page_has_pii(
    blocks: list[ExtractedBlock],
    page: int | str,
    engine: "PresidioEngine",
) -> bool:
    """Run Presidio on blocks from a single page, return True if high-confidence PII found."""
    page_blocks = [b for b in blocks if b.page_or_sheet == page]
    if not page_blocks:
        return False
    detections = engine.analyze(page_blocks)
    return any(d.score >= _PII_VERIFICATION_SCORE for d in detections)


def find_verified_onset(
    blocks: list[ExtractedBlock],
    file_type: str,
    engine: "PresidioEngine",
) -> int | str:
    """Two-pass onset detection: heuristic candidates then Presidio verification.

    For tabular files (CSV/Excel/Parquet), always returns 0.

    Pass 1 (Heuristic): scan blocks for ONSET_SIGNALS text patterns.
      Returns up to 5 candidate pages.

    Pass 2 (PII Verification): for each candidate page (and the page after it),
      run PresidioEngine.analyze(). If any detection has score >= 0.70, that's
      the verified onset.

    Fallback: if no candidates had PII, sequentially scan pages 0..19
    running Presidio until PII is found.

    If still no PII found, returns 0.

    Parameters
    ----------
    blocks:
        All ExtractedBlock objects for the document.
    file_type:
        File extension (e.g. "pdf", "docx", "csv").
    engine:
        PresidioEngine instance for PII verification.

    Returns
    -------
    int | str
        The page_or_sheet value of the verified onset page.
    """
    if file_type.lower() in _TABULAR:
        return 0

    if not blocks:
        return 0

    # Collect all distinct pages in order
    all_pages: list[int | str] = []
    seen_pages: set[int | str] = set()
    for b in blocks:
        if b.page_or_sheet not in seen_pages:
            seen_pages.add(b.page_or_sheet)
            all_pages.append(b.page_or_sheet)

    # Pass 1: heuristic candidates
    candidates = _get_heuristic_candidate_pages(blocks)

    # Pass 2: verify candidates with Presidio
    for candidate in candidates:
        if _verify_page_has_pii(blocks, candidate, engine):
            logger.info(
                "Verified onset: page %s (heuristic candidate confirmed by Presidio)",
                candidate,
            )
            return candidate
        # Also check the next page (data may start on the page after the keyword header)
        idx = all_pages.index(candidate) if candidate in all_pages else -1
        if idx >= 0 and idx + 1 < len(all_pages):
            next_page = all_pages[idx + 1]
            if _verify_page_has_pii(blocks, next_page, engine):
                logger.info(
                    "Verified onset: page %s (next page after heuristic candidate %s)",
                    next_page, candidate,
                )
                return next_page

    # Fallback: sequential scan of first N pages
    checked = set(candidates)
    # Also mark candidate+1 pages as checked
    for c in candidates:
        idx = all_pages.index(c) if c in all_pages else -1
        if idx >= 0 and idx + 1 < len(all_pages):
            checked.add(all_pages[idx + 1])

    scan_count = 0
    for page in all_pages:
        if page in checked:
            continue
        if scan_count >= _MAX_SEQUENTIAL_SCAN:
            break
        if _verify_page_has_pii(blocks, page, engine):
            logger.info(
                "Verified onset: page %s (found via sequential scan)",
                page,
            )
            return page
        scan_count += 1

    # No PII found anywhere — fall back to beginning
    logger.info("No verified PII onset found; defaulting to page 0")
    return 0


def find_verified_onset_pdf(
    fitz_doc: object,
    engine: "PresidioEngine",
) -> int:
    """Memory-efficient PII-verified onset for PDFs using fitz page streaming.

    Same two-pass logic as find_verified_onset() but reads pages on demand
    and calls _forget_page() to release memory immediately.

    Parameters
    ----------
    fitz_doc:
        A fitz (PyMuPDF) Document object.
    engine:
        PresidioEngine instance for PII verification.

    Returns
    -------
    int
        The 0-based page number of the verified onset page.
    """
    page_count = len(fitz_doc)
    if page_count == 0:
        return 0

    # Pass 1: heuristic scan — find candidate pages with onset signals
    candidates: list[int] = []
    for page_num in range(page_count):
        text = fitz_doc.load_page(page_num).get_text()
        fitz_doc._forget_page(page_num)
        if any(p.search(text) for p in _COMPILED_SIGNALS):
            candidates.append(page_num)
            if len(candidates) >= _MAX_HEURISTIC_CANDIDATES:
                break

    # Pass 2: verify candidates with Presidio
    def _check_pdf_page(pg: int) -> bool:
        if pg < 0 or pg >= page_count:
            return False
        text = fitz_doc.load_page(pg).get_text()
        fitz_doc._forget_page(pg)
        if not text.strip():
            return False
        block = ExtractedBlock(
            text=text, page_or_sheet=pg,
            source_path="", file_type="pdf",
        )
        detections = engine.analyze([block])
        return any(d.score >= _PII_VERIFICATION_SCORE for d in detections)

    for candidate in candidates:
        if _check_pdf_page(candidate):
            logger.info("PDF verified onset: page %d (heuristic confirmed)", candidate)
            return candidate
        # Check next page
        if candidate + 1 < page_count:
            if _check_pdf_page(candidate + 1):
                logger.info("PDF verified onset: page %d (next after heuristic %d)", candidate + 1, candidate)
                return candidate + 1

    # Fallback: sequential scan
    checked: set[int] = set()
    for c in candidates:
        checked.add(c)
        if c + 1 < page_count:
            checked.add(c + 1)

    scan_count = 0
    for pg in range(page_count):
        if pg in checked:
            continue
        if scan_count >= _MAX_SEQUENTIAL_SCAN:
            break
        if _check_pdf_page(pg):
            logger.info("PDF verified onset: page %d (sequential scan)", pg)
            return pg
        scan_count += 1

    logger.info("PDF: no verified PII onset found; defaulting to page 0")
    return 0
