"""Cataloger task: classify documents by structure class after discovery.

Runs AFTER discovery, BEFORE extraction.  Each document is assigned a
``structure_class`` based on its file extension:

    structured       -- csv, xlsx, xls, parquet, avro  (tabular data)
    semi-structured  -- html, htm, xml, eml, msg       (markup / email)
    unstructured     -- pdf, docx                      (free-form text)
    non-extractable  -- unknown extensions, corrupt, .dat, no reader

Also sets:
    can_auto_process     -- True unless non-extractable or corrupt
    manual_review_reason -- human-readable reason when can_auto_process=False

Non-extractable documents are routed to the manual review queue via the
existing ``QueueManager``.
"""
from __future__ import annotations

import logging
from typing import Literal

from sqlalchemy.orm import Session

from app.db.models import Document

logger = logging.getLogger(__name__)

StructureClass = Literal["structured", "semi-structured", "unstructured", "non-extractable"]

# ---- Extension classification map ----------------------------------------

_STRUCTURED_EXTENSIONS: frozenset[str] = frozenset({
    "csv", "xlsx", "xls", "parquet", "avro",
})

_SEMI_STRUCTURED_EXTENSIONS: frozenset[str] = frozenset({
    "html", "htm", "xml", "eml", "msg",
})

_UNSTRUCTURED_EXTENSIONS: frozenset[str] = frozenset({
    "pdf", "docx",
})

# All extensions that have a known reader (union of the three classes above)
_ALL_KNOWN_EXTENSIONS: frozenset[str] = (
    _STRUCTURED_EXTENSIONS | _SEMI_STRUCTURED_EXTENSIONS | _UNSTRUCTURED_EXTENSIONS
)


def classify_extension(ext: str) -> StructureClass:
    """Return the structure class for a lowercase, dot-stripped extension.

    Parameters
    ----------
    ext:
        Lowercase extension without leading dot (e.g. ``"pdf"``).

    Returns
    -------
    StructureClass
        One of ``"structured"``, ``"semi-structured"``, ``"unstructured"``,
        or ``"non-extractable"``.
    """
    ext = ext.lower().strip()
    if ext in _STRUCTURED_EXTENSIONS:
        return "structured"
    if ext in _SEMI_STRUCTURED_EXTENSIONS:
        return "semi-structured"
    if ext in _UNSTRUCTURED_EXTENSIONS:
        return "unstructured"
    return "non-extractable"


# ---- CatalogerTask -------------------------------------------------------


class CatalogerTask:
    """Classify discovered documents and update their catalog fields.

    Usage::

        cataloger = CatalogerTask(db_session)
        results = cataloger.run(documents)

    Each ``Document`` ORM object in *documents* is updated in-place with
    ``structure_class``, ``can_auto_process``, and ``manual_review_reason``.
    The session is flushed once at the end.
    """

    def __init__(self, db_session: Session) -> None:
        self.db = db_session

    def run(self, documents: list[Document]) -> list[Document]:
        """Classify each document and persist catalog fields.

        Parameters
        ----------
        documents:
            ORM Document objects that have been discovered but not yet
            cataloged (``structure_class IS NULL``).

        Returns
        -------
        list[Document]
            The same list, with catalog fields populated.
        """
        for doc in documents:
            self._classify(doc)

        self.db.flush()

        # Log summary
        counts: dict[str, int] = {}
        for doc in documents:
            sc = doc.structure_class or "unknown"
            counts[sc] = counts.get(sc, 0) + 1
        logger.info(
            "Cataloger complete: %d documents classified — %s",
            len(documents),
            ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
        )

        return documents

    # ---- internal ---------------------------------------------------------

    def _classify(self, doc: Document) -> None:
        """Set structure_class, can_auto_process, manual_review_reason."""
        ext = (doc.file_type or "").lower().strip()

        structure_class = classify_extension(ext)
        doc.structure_class = structure_class

        if structure_class == "non-extractable":
            doc.can_auto_process = False
            doc.manual_review_reason = (
                f"File type '.{ext}' is not supported for automated extraction"
                if ext
                else "File has no recognized extension"
            )
        else:
            doc.can_auto_process = True
            doc.manual_review_reason = None
