"""Cross-page entity stitcher using a tail-buffer pattern.

Maintains a rolling buffer of the last TAIL_BUFFER_LINES lines from the
previous page and prepends it to the current page text before PII extraction.
This ensures PII that spans a page boundary is captured as a single entity.

Usage
-----
    stitcher = PageStitcher()
    for page_num, page_text in enumerate(pages):
        stitched_text, tail_len = stitcher.stitch(page_num, page_text)
        results = extract_pii(stitched_text)
        for r in results:
            r.spans_pages = (page_num - 1, page_num) if r.start_char < tail_len else None

Spans rule (from CLAUDE.md § 2):
    A PII result whose start_char < tail_len originated in the previous
    page's tail and therefore spans pages.  Callers must set:
        spans_pages = (page_num - 1, page_num)   if start_char < tail_len
        spans_pages = None                         otherwise

Excel rule: call reset() before each new worksheet so context never
bleeds across tab boundaries.
"""
from __future__ import annotations

TAIL_BUFFER_LINES: int = 5


class PageStitcher:
    """Stateful stitcher; one instance per document.

    Not thread-safe: create one instance per concurrent document worker.
    """

    def __init__(self) -> None:
        self._tail_buffer: list[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def stitch(self, page_num: int, page_text: str) -> tuple[str, int]:
        """Prepend the previous page's tail buffer and return stitched text.

        Parameters
        ----------
        page_num:
            0-based index of the current page (used only by the caller
            for spans_pages labelling; stitcher does not use it internally).
        page_text:
            Plain text extracted from the current page (output of
            page.get_text() or equivalent OCR text).

        Returns
        -------
        stitched_text:
            If a tail buffer exists: ``tail_text + "\\n" + page_text``.
            On the first page (or after reset()): ``page_text`` unchanged.
        tail_buffer_len:
            ``len(tail_text)`` — the number of characters occupied by the
            prepended tail portion (NOT including the joining newline).
            Any PII result with ``start_char < tail_buffer_len`` spans
            the boundary between page_num-1 and page_num.
            Zero on the first page or after reset().
        """
        tail_text = "\n".join(self._tail_buffer)
        tail_buffer_len = len(tail_text)

        stitched = (tail_text + "\n" + page_text) if self._tail_buffer else page_text

        # Update tail buffer: keep only the last TAIL_BUFFER_LINES lines of
        # the current page so future pages can prepend them.
        lines = page_text.splitlines()
        self._tail_buffer = lines[-TAIL_BUFFER_LINES:] if lines else []

        return stitched, tail_buffer_len

    def reset(self) -> None:
        """Clear the tail buffer.

        Must be called:
        - Between Excel worksheets (tab isolation rule)
        - Between documents when re-using the same instance
        """
        self._tail_buffer = []

    # ------------------------------------------------------------------
    # Inspection (tests and debugging only)
    # ------------------------------------------------------------------

    @property
    def tail_buffer(self) -> list[str]:
        """Read-only view of the current tail buffer (a copy)."""
        return list(self._tail_buffer)
