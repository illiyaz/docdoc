"""Apache Tika fallback reader for unsupported file formats.

Tika is self-hosted (see config.yaml readers.tika.endpoint) and must be
reachable at the configured local endpoint. The public Tika cloud service
must never be used.

TikaReader is the last resort — always prefer the dedicated reader for
known formats. The registry routes here automatically for unrecognized
extensions.
"""
from __future__ import annotations

from pathlib import Path

from app.readers.base import BaseReader, ExtractedBlock


class TikaReader(BaseReader):
    """Submit a file to the local Tika server and parse the plain-text response."""

    def __init__(self, path: str | Path) -> None:
        super().__init__(path)

    def read(self) -> list[ExtractedBlock]:
        """POST file to Tika, receive plain text, and return prose ExtractedBlocks."""
        raise NotImplementedError(f"{type(self).__name__}.read() is not yet implemented")
