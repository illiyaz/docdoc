"""Feedback-Driven Extraction Selector (A0).

Replaces the fragile Path 0/1/2/3 decision chain with a three-phase
sample-test-scale loop:

  Phase 1 — UNDERSTAND: profile the document (zero LLM calls)
  Phase 2 — COMPETE: try applicable methods on 5 sample pages
  Phase 3 — SCALE: apply the winner to all pages

See overnight_results/extraction_loop_design.md for full design rationale.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.pii.presidio_engine import PresidioEngine
    from app.readers.base import ExtractedBlock
    from app.structure.document_schema import DocumentSchema

logger = logging.getLogger(__name__)


# ============================================================
# Phase 1: UNDERSTAND — Document Profiling
# ============================================================

@dataclass
class DensityInfo:
    """PII density estimates from a sample page."""
    persons_per_page: float = 0.0
    has_ssn: bool = False
    has_addresses: bool = False
    has_dates: bool = False
    has_phones: bool = False
    has_emails: bool = False
    sample_page: int | str = 0


@dataclass
class Zone:
    """A contiguous region of the document with a consistent role."""
    start: int | str  # first page/sheet
    end: int | str    # last page/sheet
    zone_type: str    # "cover", "data", "appendix", "blank"
    page_count: int = 0


@dataclass
class DocumentProfile:
    """Everything we know about a document before extraction.

    Built entirely from PyMuPDF (text layer check), Presidio (onset),
    and heuristics (layout comparison).  Zero LLM calls.
    """
    total_pages: int = 0
    page_types: dict[int | str, str] = field(default_factory=dict)
    # page_types values: "text", "scanned", "mixed", "blank"

    onset_page: int | str = 0          # where PII data actually starts
    zones: list[Zone] = field(default_factory=list)
    density: DensityInfo = field(default_factory=DensityInfo)
    is_uniform: bool = True            # same layout across data pages?
    text_ratio: float = 1.0            # fraction of pages with text layer
    has_text_layer: bool = True        # any text at all?
    file_type: str = ""
    file_name: str = ""
    source_path: str = ""
    profiling_time_ms: float = 0.0


def classify_page_types(doc_path: str) -> dict[int, str]:
    """Classify each PDF page as text, scanned, mixed, or blank.

    Uses PyMuPDF text layer check.  Returns empty dict for non-PDFs.
    """
    result: dict[int, str] = {}
    try:
        import fitz
        doc = fitz.open(doc_path)
        for page_num in range(doc.page_count):
            page = doc[page_num]
            text = page.get_text("text").strip()
            images = page.get_images()

            if not text and not images:
                result[page_num] = "blank"
            elif not text and images:
                result[page_num] = "scanned"
            elif text and images and len(text) < 50:
                result[page_num] = "mixed"  # mostly image with minimal text
            else:
                result[page_num] = "text"
        doc.close()
    except ImportError:
        logger.debug("fitz not available for page classification")
    except Exception as e:
        logger.debug("Page classification failed for %s: %s", doc_path, e)
    return result


def estimate_density(
    blocks: list[ExtractedBlock],
    onset_page: int | str,
    engine: PresidioEngine | None = None,
) -> DensityInfo:
    """Estimate PII density from a single page (the onset page).

    Counts Presidio detections if engine is available, otherwise
    falls back to regex pattern matching.
    """
    # Gather text from the onset page
    page_blocks = [b for b in blocks if b.page_or_sheet == onset_page]
    if not page_blocks:
        # Try numeric comparison for string page values
        page_blocks = [b for b in blocks if str(b.page_or_sheet) == str(onset_page)]

    page_text = "\n".join(b.text for b in page_blocks)

    info = DensityInfo(sample_page=onset_page)

    if engine is not None:
        try:
            detections = engine.analyze(page_text)
            person_names = [d for d in detections if d.entity_type == "PERSON" and d.score >= 0.70]
            info.persons_per_page = len(person_names)
            info.has_ssn = any(d.entity_type in ("US_SSN", "SSN") for d in detections)
            info.has_addresses = any(d.entity_type in ("ADDRESS", "LOCATION") for d in detections)
            info.has_dates = any(d.entity_type in ("DATE_TIME", "DATE_OF_BIRTH") for d in detections)
            info.has_phones = any(d.entity_type == "PHONE_NUMBER" for d in detections)
            info.has_emails = any(d.entity_type == "EMAIL_ADDRESS" for d in detections)
            return info
        except Exception:
            pass  # Fall through to regex

    # Regex fallback — rough estimate
    # Count likely person name patterns (Title Case sequences)
    name_pattern = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}\b', page_text)
    info.persons_per_page = len(name_pattern)
    info.has_ssn = bool(re.search(r'\d{3}-\d{2}-\d{4}', page_text))
    info.has_dates = bool(re.search(r'\d{1,2}/\d{1,2}/\d{2,4}', page_text))
    info.has_phones = bool(re.search(r'\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}', page_text))
    info.has_emails = bool(re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', page_text))

    return info


def detect_zones(
    blocks: list[ExtractedBlock],
    onset_page: int | str,
    total_pages: int = 0,
) -> list[Zone]:
    """Detect document zones: cover (pre-onset), data (onset+), appendix.

    Simple heuristic: everything before onset is cover, everything from
    onset onward is data.  Future: detect appendix from density drop-off.
    """
    zones: list[Zone] = []

    if isinstance(onset_page, int) and onset_page > 0:
        zones.append(Zone(
            start=0,
            end=onset_page - 1,
            zone_type="cover",
            page_count=onset_page,
        ))

    data_end = total_pages - 1 if total_pages > 0 else onset_page
    if isinstance(onset_page, int) and isinstance(data_end, int):
        zones.append(Zone(
            start=onset_page,
            end=data_end,
            zone_type="data",
            page_count=data_end - onset_page + 1,
        ))
    else:
        zones.append(Zone(
            start=onset_page,
            end=data_end,
            zone_type="data",
        ))

    return zones


def check_layout_consistency(
    doc_path: str,
    onset_page: int,
    sample_count: int = 3,
) -> bool:
    """Check if data pages have a uniform layout.

    Compares text block positions across sample data pages.
    Returns True if layouts are similar (good for coordinate extraction).
    """
    try:
        import fitz
        doc = fitz.open(doc_path)

        if doc.page_count <= onset_page:
            doc.close()
            return True

        # Sample onset + next N pages
        sample_pages = list(range(
            onset_page,
            min(onset_page + sample_count, doc.page_count),
        ))

        if len(sample_pages) < 2:
            doc.close()
            return True

        # Get text block count per page as a rough layout fingerprint
        block_counts = []
        for pn in sample_pages:
            page = doc[pn]
            tblocks = page.get_text("dict", flags=0)
            block_counts.append(len(tblocks.get("blocks", [])))

        doc.close()

        if not block_counts:
            return True

        # If block counts vary by more than 50%, layout is inconsistent
        avg = sum(block_counts) / len(block_counts)
        if avg == 0:
            return True
        max_deviation = max(abs(c - avg) / avg for c in block_counts)
        return max_deviation < 0.5

    except ImportError:
        return True  # Can't check without fitz, assume uniform
    except Exception:
        return True


def build_document_profile(
    doc_path: str,
    blocks: list[ExtractedBlock],
    file_type: str,
    file_name: str,
    onset_page: int | str = 0,
    engine: PresidioEngine | None = None,
) -> DocumentProfile:
    """Build a complete DocumentProfile.  Zero LLM calls.

    Parameters
    ----------
    doc_path : str
        Path to the source file on disk.
    blocks : list[ExtractedBlock]
        All text blocks from the reader.
    file_type : str
        File extension (e.g. "pdf", "docx", "xlsx").
    file_name : str
        Human-readable filename.
    onset_page : int | str
        Pre-computed onset page (from find_verified_onset).
    engine : PresidioEngine | None
        Optional Presidio engine for density estimation.

    Returns
    -------
    DocumentProfile
        Complete profile ready for Phase 2 competition.
    """
    _start = time.time()

    is_pdf = file_type.lower() in ("pdf", ".pdf", "application/pdf")

    # 1. Page classification (PDF only)
    page_types: dict[int | str, str] = {}
    total_pages = 0
    if is_pdf:
        page_types = classify_page_types(doc_path)
        total_pages = len(page_types) if page_types else 0
    if total_pages == 0:
        # Non-PDF or classification failed — count from blocks
        all_pages = set(b.page_or_sheet for b in blocks)
        total_pages = len(all_pages) if all_pages else 1

    # 2. Text ratio
    text_pages = sum(1 for t in page_types.values() if t in ("text", "mixed"))
    text_ratio = text_pages / total_pages if total_pages > 0 and page_types else 1.0
    has_text_layer = text_ratio > 0.0 or bool(blocks)

    # 3. Density estimation
    density = estimate_density(blocks, onset_page, engine)

    # 4. Zone detection
    _onset_int = onset_page if isinstance(onset_page, int) else 0
    zones = detect_zones(blocks, onset_page, total_pages)

    # 5. Layout consistency (PDF only, needs int onset)
    is_uniform = True
    if is_pdf and isinstance(onset_page, int):
        is_uniform = check_layout_consistency(doc_path, onset_page)

    _elapsed_ms = (time.time() - _start) * 1000

    profile = DocumentProfile(
        total_pages=total_pages,
        page_types=page_types,
        onset_page=onset_page,
        zones=zones,
        density=density,
        is_uniform=is_uniform,
        text_ratio=text_ratio,
        has_text_layer=has_text_layer,
        file_type=file_type,
        file_name=file_name,
        source_path=doc_path,
        profiling_time_ms=_elapsed_ms,
    )

    logger.info(
        "PROFILE: %s | pages=%d | text_ratio=%.2f | onset=%s | density=%.1f/page | uniform=%s | zones=%d | %.0fms",
        file_name, total_pages, text_ratio, onset_page,
        density.persons_per_page, is_uniform, len(zones), _elapsed_ms,
    )

    return profile


# ============================================================
# Phase 2 helper: pick sample pages from data zone
# ============================================================

def pick_sample_pages(
    profile: DocumentProfile,
    blocks: list[ExtractedBlock],
    n: int = 5,
) -> list[int | str]:
    """Pick n sample pages from the data zone for method competition.

    Starts from onset_page, skips blank/scanned pages if text methods
    are being tested.  Returns up to n pages.
    """
    # Find the data zone
    data_zone = None
    for z in profile.zones:
        if z.zone_type == "data":
            data_zone = z
            break

    if data_zone is None:
        # No zones detected — use all pages from onset
        all_pages = sorted(set(b.page_or_sheet for b in blocks))
        onset_idx = 0
        for idx, p in enumerate(all_pages):
            if str(p) == str(profile.onset_page):
                onset_idx = idx
                break
        return all_pages[onset_idx:onset_idx + n]

    # Build candidate list from data zone
    if isinstance(data_zone.start, int) and isinstance(data_zone.end, int):
        candidates = list(range(data_zone.start, data_zone.end + 1))
    else:
        # Non-integer pages (sheets) — gather from blocks
        candidates = sorted(set(
            b.page_or_sheet for b in blocks
            if str(b.page_or_sheet) >= str(data_zone.start)
        ))

    # Filter out blank pages
    if profile.page_types:
        candidates = [
            p for p in candidates
            if profile.page_types.get(p, "text") != "blank"
        ]

    # For large docs, spread samples across the zone instead of just first N
    if len(candidates) > n * 10:
        step = len(candidates) // n
        sampled = [candidates[i * step] for i in range(n)]
        return sampled

    return candidates[:n]
