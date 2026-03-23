"""Discovery task: scan data sources and catalog documents for processing.

Implements the pluggable DataSourceConnector interface.  Each connector
(filesystem, PostgreSQL, MinIO, MongoDB, email) is independent — the
pipeline does not know or care which connector sourced a document.

Connectors in scope for Phase 1: FilesystemConnector.
PostgresConnector is stubbed (Phase 2).

Every discovered file is returned as a DocumentInfo dict with the fields
needed to create a Document ORM record:

    source_path  : absolute path string
    file_name    : filename component
    file_type    : lowercase extension without dot
    size_bytes   : file size in bytes
    sha256       : SHA-256 hex digest of file content

Air-gap rule: no outbound network calls are made by FilesystemConnector.
"""
from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)

# Extensions that the reader registry handles with a dedicated reader.
# Unknown extensions fall back to TikaReader — also valid to discover.
_KNOWN_EXTENSIONS: frozenset[str] = frozenset({
    # Documents
    "pdf", "docx", "xlsx", "xls", "xlsm", "csv", "tsv",
    "html", "htm", "xml", "eml", "msg", "parquet", "avro",
    # Images (Step 23b — vision-first extraction)
    "jpg", "jpeg", "png", "bmp", "webp", "gif",
    "heic", "heif", "tif", "tiff",
    # Archives (extracted during discovery or upload)
    "zip", "7z",
})

# Archive extensions that need pre-extraction before reader processing
_ARCHIVE_EXTENSIONS: frozenset[str] = frozenset({"zip", "7z"})


class DocumentInfo(TypedDict):
    """Minimal document descriptor returned by every connector."""
    source_path: str
    file_name: str
    file_type: str
    size_bytes: int
    sha256: str


class DataSourceConnector(ABC):
    """Pluggable interface for data source adapters."""

    @abstractmethod
    def list_documents(self) -> list[DocumentInfo]:
        """Return a list of DocumentInfo dicts for all discoverable documents."""
        ...

    @abstractmethod
    def fetch_document(self, doc_id: str) -> bytes:
        """Fetch raw document bytes by source_path (used as doc_id)."""
        ...


class FilesystemConnector(DataSourceConnector):
    """Scan a local directory tree for supported document types.

    Parameters
    ----------
    root:
        Root directory to scan recursively.
    extensions:
        If provided, only files with these (lowercase, no-dot) extensions are
        discovered.  Defaults to all _KNOWN_EXTENSIONS.
    """

    def __init__(
        self,
        root: str | Path,
        extensions: frozenset[str] | None = None,
    ) -> None:
        self.root = Path(root)
        self.extensions = extensions if extensions is not None else _KNOWN_EXTENSIONS

    def list_documents(self) -> list[DocumentInfo]:
        """Recursively walk root and return a DocumentInfo for every matching file.

        Archive files (.zip, .7z) are pre-extracted into subdirectories and
        their contents are included in the result.  The archive files
        themselves are excluded (they have no reader).
        """
        # Pass 1: extract any archives found in the tree
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            ext = path.suffix.lstrip(".").lower()
            if ext in _ARCHIVE_EXTENSIONS and (not self.extensions or ext in self.extensions):
                self._extract_archive_for_discovery(path)

        # Pass 2: discover all non-archive files (including extracted ones)
        docs: list[DocumentInfo] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            ext = path.suffix.lstrip(".").lower()
            if self.extensions and ext not in self.extensions:
                continue
            if ext in _ARCHIVE_EXTENSIONS:
                continue  # skip archive files themselves
            try:
                docs.append(self._describe(path))
            except OSError as exc:
                logger.warning("Skipping %s: %s", path, exc)
        return docs

    def fetch_document(self, doc_id: str) -> bytes:
        """Read and return the bytes of the file at doc_id (treated as a path)."""
        return Path(doc_id).read_bytes()

    @staticmethod
    def _extract_archive_for_discovery(archive_path: Path) -> list[Path]:
        """Extract archive contents to a subdirectory next to the archive.

        Creates ``<archive_stem>_extracted/`` next to the archive file,
        extracts supported files into it, and returns extracted file paths.
        Reuses ``upload_helpers.extract_archive()`` — no code duplication.

        If already extracted (directory exists and is non-empty), reuses
        existing files without re-extracting.  Archive file itself is NOT
        deleted (kept for audit trail).

        Returns empty list on failure (bad archive, missing py7zr).
        """
        dest_dir = archive_path.parent / f"{archive_path.stem}_extracted"

        # Reuse existing extraction if already done (idempotent)
        if dest_dir.is_dir() and any(dest_dir.iterdir()):
            return [
                f for f in sorted(dest_dir.rglob("*"))
                if f.is_file()
                and f.suffix.lstrip(".").lower() in _KNOWN_EXTENSIONS
                and f.suffix.lstrip(".").lower() not in _ARCHIVE_EXTENSIONS
            ]

        dest_dir.mkdir(exist_ok=True)
        try:
            from app.api.upload_helpers import extract_archive
            extracted = extract_archive(archive_path, dest_dir)
        except Exception as exc:
            logger.warning("Failed to extract archive %s: %s", archive_path, exc)
            return []

        return [
            dest_dir / item["name"]
            for item in extracted
            if (dest_dir / item["name"]).is_file()
        ]

    @staticmethod
    def _describe(path: Path) -> DocumentInfo:
        content = path.read_bytes()
        return DocumentInfo(
            source_path=str(path.resolve()),
            file_name=path.name,
            file_type=path.suffix.lstrip(".").lower(),
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )


class PostgresConnector(DataSourceConnector):
    """Treat rows in a configured PostgreSQL table as documents.

    Phase 2 — not yet implemented.  Requires a DB session and knowledge of
    the target table schema.  Raises NotImplementedError until Phase 2.
    """

    def __init__(self, table: str, content_column: str) -> None:
        self.table = table
        self.content_column = content_column

    def list_documents(self) -> list[DocumentInfo]:
        raise NotImplementedError(
            "PostgresConnector.list_documents() is not implemented until Phase 2."
        )

    def fetch_document(self, doc_id: str) -> bytes:
        raise NotImplementedError(
            "PostgresConnector.fetch_document() is not implemented until Phase 2."
        )


class DiscoveryTask:
    """Catalog documents from all configured connectors.

    Deduplicates by SHA-256 so the same file discovered via two connectors
    is only cataloged once.
    """

    def run(self, connectors: list[DataSourceConnector]) -> list[DocumentInfo]:
        """Scan all connectors; return deduplicated list of DocumentInfo dicts.

        Parameters
        ----------
        connectors:
            One or more DataSourceConnector instances to scan.

        Returns
        -------
        list[DocumentInfo]
            Unique documents ordered by source_path, deduplicated by sha256.
        """
        seen_hashes: set[str] = set()
        all_docs: list[DocumentInfo] = []

        for connector in connectors:
            try:
                docs = connector.list_documents()
            except Exception as exc:
                logger.error("Connector %s failed: %s", type(connector).__name__, exc)
                continue

            for doc in docs:
                if doc["sha256"] in seen_hashes:
                    logger.debug(
                        "Skipping duplicate sha256=%s path=%s",
                        doc["sha256"][:12],
                        doc["source_path"],
                    )
                    continue
                seen_hashes.add(doc["sha256"])
                all_docs.append(doc)

        logger.info("Discovery complete: %d unique documents found.", len(all_docs))
        return sorted(all_docs, key=lambda d: d["source_path"])