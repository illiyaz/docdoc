"""Email reader: handles both .eml (stdlib) and .msg (extract-msg) formats.

Extracts text from email body (plain text, HTML, RTF) into ExtractedBlock objects.
D2 enhancement: Attachments are now extracted inline via the reader registry.
PDF, XLSX, DOCX, CSV, and image attachments are saved to a temp directory
and processed through the appropriate reader, with blocks attributed to
the parent email source.

Step 23b enhancement: .msg support via extract-msg library.
Proven March 2026: MSG emails with PII in body text (not attachments)
now extract 6-21 records per email (was 0 before).

page_or_sheet is set to the MIME part index. bbox is None for all blocks.
"""
from __future__ import annotations

import email as _email_lib
import logging
import os
import re
import tempfile
from email import policy as _email_policy
from pathlib import Path

logger = logging.getLogger(__name__)

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
        """Parse .eml file using stdlib email, including attachments (D2)."""
        source = str(self.path)
        raw = self.path.read_bytes()
        msg = _email_lib.message_from_bytes(raw, policy=_email_policy.default)

        blocks: list[ExtractedBlock] = []
        part_index = 0

        if msg.is_multipart():
            for part in msg.walk():
                disposition = str(part.get("Content-Disposition", ""))
                content_type = part.get_content_type()

                if "attachment" in disposition:
                    # D2: Extract attachment content via reader registry
                    att_blocks = self._extract_attachment_part(part, part_index, source)
                    blocks.extend(att_blocks)
                    part_index += 1
                    continue

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

        # D2: Extract attachments via reader registry
        try:
            for att in getattr(msg, "attachments", []):
                att_name = getattr(att, "longFilename", None) or getattr(att, "shortFilename", None) or "attachment"
                att_data = getattr(att, "data", None)
                if att_data and att_name:
                    att_blocks = self._extract_attachment_bytes(att_name, att_data, source)
                    blocks.extend(att_blocks)
        except Exception:
            logger.debug("Failed to extract MSG attachments from %s", source, exc_info=True)

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

    # -----------------------------------------------------------------
    # D2: Attachment extraction helpers
    # -----------------------------------------------------------------

    # Extensions we know how to process via the reader registry
    _PROCESSABLE_EXTENSIONS = {
        ".pdf", ".xlsx", ".xls", ".csv", ".docx", ".doc", ".txt",
        ".html", ".htm", ".xml", ".eml", ".msg",
        ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".heic", ".heif",
    }

    def _extract_attachment_part(
        self, part: object, part_index: int, source_path: str,
    ) -> list[ExtractedBlock]:
        """Extract an EML MIME attachment by saving to temp and reading."""
        filename = getattr(part, "get_filename", lambda: None)()
        if not filename:
            return []

        ext = Path(filename).suffix.lower()
        if ext not in self._PROCESSABLE_EXTENSIONS:
            return []

        try:
            payload = part.get_payload(decode=True)
            if not payload:
                return []
            return self._extract_attachment_bytes(filename, payload, source_path)
        except Exception:
            logger.debug("Failed to extract EML attachment %s", filename, exc_info=True)
            return []

    def _extract_attachment_bytes(
        self, filename: str, data: bytes, parent_source: str,
    ) -> list[ExtractedBlock]:
        """Save attachment bytes to a temp file, read via registry, return blocks."""
        ext = Path(filename).suffix.lower()
        if ext not in self._PROCESSABLE_EXTENSIONS:
            return []

        try:
            from app.readers.registry import get_reader

            # Save to temp file
            with tempfile.NamedTemporaryFile(
                suffix=ext, prefix="att_", delete=False,
            ) as tmp:
                tmp.write(data)
                tmp_path = tmp.name

            try:
                reader = get_reader(tmp_path)
                att_blocks = reader.read()

                # Re-attribute blocks to the parent email source
                result: list[ExtractedBlock] = []
                for block in att_blocks:
                    result.append(ExtractedBlock(
                        text=block.text,
                        page_or_sheet=block.page_or_sheet,
                        source_path=parent_source,
                        file_type=block.file_type,
                        block_type=block.block_type,
                        bbox=block.bbox,
                        row=block.row,
                        column=block.column,
                        table_id=block.table_id,
                    ))

                if result:
                    logger.info(
                        "Extracted %d blocks from attachment %s in %s",
                        len(result), filename, parent_source,
                    )
                return result
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        except ImportError:
            return []
        except Exception:
            logger.debug("Failed to read attachment %s", filename, exc_info=True)
            return []
