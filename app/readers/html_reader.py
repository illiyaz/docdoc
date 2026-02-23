"""HTML / XML reader: BeautifulSoup4 tag-stripped extraction.

All tags are stripped before PII extraction. The original HTML structure
is preserved as metadata for potential Layer 3 positional inference.
bbox is None for all blocks (non-visual format).
page_or_sheet is set to 0 (HTML has no page concept).
"""
from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup

from app.readers.base import BaseReader, ExtractedBlock

# Tags whose content is removed entirely (not just the tag itself)
_SKIP_TAGS = {"script", "style", "head", "meta", "link"}


class HTMLReader(BaseReader):
    """Extract visible text from an HTML or XML file."""

    def __init__(self, path: str | Path) -> None:
        super().__init__(path)

    def read(self) -> list[ExtractedBlock]:
        """Strip HTML tags and yield prose ExtractedBlock objects."""
        source = str(self.path)
        file_type = self.path.suffix.lstrip(".").lower() or "html"

        raw = self.path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(raw, "html.parser")

        # Remove elements whose content should never be extracted
        for tag in soup(_SKIP_TAGS):
            tag.decompose()

        text = soup.get_text(separator="\n")
        lines = [line.strip() for line in text.splitlines() if line.strip()]

        return [
            ExtractedBlock(
                text=line,
                page_or_sheet=0,
                source_path=source,
                file_type=file_type,
                block_type="prose",
                bbox=None,
            )
            for line in lines
        ]
