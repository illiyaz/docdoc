"""Thin wrapper around LiteParse Python library for spatial text extraction."""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def get_spatial_text(doc_path: str, page_num: int = 0) -> str | None:
    """Extract spatial text from a single PDF page using LiteParse.

    Returns the layout-preserved text string, or None if LiteParse
    is not installed or fails.
    """
    try:
        from liteparse import LiteParse
        parser = LiteParse()
        result = parser.parse(doc_path, max_pages=page_num + 1)
        if result and result.pages and len(result.pages) > page_num:
            return result.pages[page_num].text
        return None
    except ImportError:
        logger.debug("LiteParse not installed — falling back to vision model")
        return None
    except Exception:
        logger.debug("LiteParse parse failed for %s page %d", doc_path, page_num, exc_info=True)
        return None


def is_available() -> bool:
    """Check if LiteParse is installed and working."""
    try:
        from liteparse import LiteParse
        LiteParse()
        return True
    except Exception:
        return False
