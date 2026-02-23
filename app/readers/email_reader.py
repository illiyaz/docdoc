"""Email reader: stdlib email parser + BeautifulSoup4 HTML body extraction.

Handles plain-text and HTML email bodies. MIME attachments are cataloged
and enqueued for independent processing through the reader registry —
they are not extracted inline.

page_or_sheet is set to the MIME part index. bbox is None for all blocks.
"""
from __future__ import annotations

import email as _email_lib
from email import policy as _email_policy
from pathlib import Path

from bs4 import BeautifulSoup

from app.readers.base import BaseReader, ExtractedBlock

_BODY_CONTENT_TYPES = {"text/plain", "text/html"}


class EmailReader(BaseReader):
    """Parse an .eml file and emit ExtractedBlock objects for body content."""

    def __init__(self, path: str | Path) -> None:
        super().__init__(path)

    def read(self) -> list[ExtractedBlock]:
        """Extract text from email body parts; skip and catalog attachments."""
        source = str(self.path)
        raw = self.path.read_bytes()
        msg = _email_lib.message_from_bytes(raw, policy=_email_policy.default)

        blocks: list[ExtractedBlock] = []
        part_index = 0

        if msg.is_multipart():
            for part in msg.walk():
                disposition = str(part.get("Content-Disposition", ""))
                if "attachment" in disposition:
                    # Attachments are cataloged but not extracted inline
                    continue
                content_type = part.get_content_type()
                if content_type in _BODY_CONTENT_TYPES:
                    blocks.extend(self._extract_part(part, part_index, source))
                    part_index += 1
        else:
            blocks.extend(self._extract_part(msg, 0, source))

        return blocks

    def _extract_part(
        self,
        part: object,
        part_index: int,
        source_path: str,
    ) -> list[ExtractedBlock]:
        """Extract content from a single MIME part (text/plain or text/html)."""
        content_type = part.get_content_type()
        file_type = self.path.suffix.lstrip(".").lower() or "eml"

        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                return []
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
        except Exception:
            return []

        if content_type == "text/html":
            soup = BeautifulSoup(text, "html.parser")
            for tag in soup(["script", "style"]):
                tag.decompose()
            text = soup.get_text(separator="\n")

        lines = [line.strip() for line in text.splitlines() if line.strip()]

        return [
            ExtractedBlock(
                text=line,
                page_or_sheet=part_index,
                source_path=source_path,
                file_type=file_type,
                block_type="prose",
                bbox=None,
            )
            for line in lines
        ]
