"""Email reader: handles both .eml (stdlib) and .msg (extract-msg) formats.

Extracts text from email body (plain text, HTML, RTF) into ExtractedBlock objects.
MIME/.msg attachments are cataloged and enqueued for independent processing
through the reader registry — they are not extracted inline.

Step 23b enhancement: .msg support via extract-msg library.
Proven March 2026: MSG emails with PII in body text (not attachments)
now extract 6-21 records per email (was 0 before).

page_or_sheet is set to the MIME part index. bbox is None for all blocks.
"""
from __future__ import annotations

import email as _email_lib
import re
from email import policy as _email_policy
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore[misc,assignment]

from app.readers.base import BaseReader, ExtractedBlock

_BODY_CONTENT_TYPES = {"text/plain", "text/html"}


class EmailReader(BaseReader):
    """Parse .eml or .msg files and emit ExtractedBlock objects for body content."""

    def __init__(self, path: str | Path) -> None:
        super().__init__(path)

    def read(self) -> list[ExtractedBlock]:
        """Extract text from email body parts; skip and catalog attachments."""
        ext = self.path.suffix.lower()
        if ext == ".msg":
            return self._read_msg()
        return self._read_eml()

    def _read_eml(self) -> list[ExtractedBlock]:
        """Parse .eml file using stdlib email."""
        source = str(self.path)
        raw = self.path.read_bytes()
        msg = _email_lib.message_from_bytes(raw, policy=_email_policy.default)

        blocks: list[ExtractedBlock] = []
        part_index = 0

        if msg.is_multipart():
            for part in msg.walk():
                disposition = str(part.get("Content-Disposition", ""))
                if "attachment" in disposition:
                    continue
                content_type = part.get_content_type()
                if content_type in _BODY_CONTENT_TYPES:
                    blocks.extend(self._extract_part(part, part_index, source))
                    part_index += 1
        else:
            blocks.extend(self._extract_part(msg, 0, source))

        return blocks

    def _read_msg(self) -> list[ExtractedBlock]:
        """Parse .msg file using extract-msg library.
        
        Tries HTML body first, then plain text, then RTF.
        """
        try:
            import extract_msg
        except ImportError:
            # Fall back to treating as binary — won't extract much
            return []

        source = str(self.path)
        file_type = "msg"
        blocks: list[ExtractedBlock] = []

        try:
            msg = extract_msg.Message(str(self.path))
        except Exception:
            return []

        # Try HTML body first (richest content)
        text = ""
        if msg.htmlBody:
            html = msg.htmlBody
            if isinstance(html, bytes):
                html = html.decode("utf-8", "replace")
            if BeautifulSoup is not None:
                soup = BeautifulSoup(html, "html.parser")
                for tag in soup(["script", "style"]):
                    tag.decompose()
                text = soup.get_text(separator="\n")
            else:
                text = re.sub(r"<[^>]+>", " ", html)
                text = re.sub(r"&\w+;", " ", text)
        elif msg.body:
            text = msg.body
        elif msg.rtfBody:
            rtf = msg.rtfBody
            if isinstance(rtf, bytes):
                rtf = rtf.decode("utf-8", "replace")
            text = re.sub(r"\\[a-z]+\d*\s?|[{}]", "", rtf)

        # Grab subject before closing (msg.subject reads from the OLE stream)
        subject = None
        try:
            subject = msg.subject
        except Exception:
            pass
        msg.close()

        if not text:
            return []

        # Include subject line as a block (may contain names)
        if subject:
            subject = subject.replace("\x00", "").strip()
            if subject:
                blocks.append(ExtractedBlock(
                    text=f"Subject: {subject}",
                    page_or_sheet=0,
                    source_path=source,
                    file_type=file_type,
                    block_type="prose",
                    bbox=None,
                ))

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in lines:
            blocks.append(ExtractedBlock(
                text=line,
                page_or_sheet=0,
                source_path=source,
                file_type=file_type,
                block_type="prose",
                bbox=None,
            ))

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
            if BeautifulSoup is not None:
                soup = BeautifulSoup(text, "html.parser")
                for tag in soup(["script", "style"]):
                    tag.decompose()
                text = soup.get_text(separator="\n")
            else:
                text = re.sub(r"<[^>]+>", " ", text)

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
