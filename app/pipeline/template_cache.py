"""Template cache for vision routing results (Step 23e).

Problem: Re-analyzing the same document layout wastes vision model time.
If 50 AWIR files have identical structure, the template from the first
should apply to all subsequent files.

Solution: Hash the onset page text + structure → cache the VisionRoutingResult
and FieldMapping. On subsequent documents with the same hash → skip vision,
reuse cached template.

Cache key: SHA-256 of (first 500 chars of onset page text, stripped of
variable content like names/dates/numbers). This captures the STRUCTURE
while ignoring the specific VALUES.

Usage:
    from app.pipeline.template_cache import TemplateCache
    
    cache = TemplateCache()
    
    # Check cache before calling vision
    cached = cache.get(doc_path, onset_page)
    if cached:
        routing, field_map = cached
    else:
        routing = router.analyze_document(...)
        field_map = builder.build_field_map(...)
        cache.put(doc_path, onset_page, routing, field_map)
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass, field

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)

# Strip variable content from text before hashing
_STRIP_PATTERNS = [
    re.compile(r"\d{3}-\d{2}-\d{4}"),         # SSN
    re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}"),   # dates
    re.compile(r"\d{4}-\d{2}-\d{2}"),          # ISO dates
    re.compile(r"\b\d{5,}\b"),                  # account numbers
    re.compile(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+"),  # mixed-case names
    re.compile(r"[A-Z]{2,}(?:,\s*)?[A-Z]{2,}"),     # ALL_CAPS names
    re.compile(r"\$[\d,.]+"),                    # dollar amounts
    re.compile(r"\b\d+\.\d+\b"),               # decimal numbers
]


@dataclass
class CacheEntry:
    """Cached vision routing result + field map."""
    routing_dict: dict  # serialized VisionRoutingResult
    field_map_dicts: list[dict] | None  # serialized FieldMapping list
    name_samples: list[str]  # PERSON samples for structural matcher
    hit_count: int = 0


class TemplateCache:
    """Cache vision analysis results by document structure fingerprint.
    
    Documents with identical layout structure (same labels, same spatial
    arrangement) get the same cache key, even if the VALUES differ.
    """
    
    def __init__(self, max_entries: int = 100) -> None:
        self._cache: dict[str, CacheEntry] = {}
        self._max_entries = max_entries
        self._lock = threading.Lock()
    
    def _compute_key(self, doc_path: str, onset_page: int) -> str | None:
        """Compute structure fingerprint from onset page text.
        
        Strips variable content (names, SSNs, dates, amounts) and hashes
        the remaining structural text (labels, headers, formatting).
        """
        try:
            doc = fitz.open(doc_path)
            if onset_page >= doc.page_count:
                doc.close()
                return None
            text = doc[onset_page].get_text()
            doc.close()
        except Exception:
            return None
        
        if not text or len(text.strip()) < 50:
            return None
        
        # Strip variable content
        structural = text[:2000]  # first 2000 chars should capture layout
        for pat in _STRIP_PATTERNS:
            structural = pat.sub("___", structural)
        
        # Normalize whitespace
        structural = re.sub(r"\s+", " ", structural).strip()
        
        # Hash
        return hashlib.sha256(structural.encode()).hexdigest()[:16]
    
    def get(
        self,
        doc_path: str,
        onset_page: int,
    ) -> CacheEntry | None:
        """Look up cached template for a document.
        
        Returns CacheEntry if found, None if miss.
        """
        key = self._compute_key(doc_path, onset_page)
        if key is None:
            return None

        with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                entry.hit_count += 1
                logger.info(
                    "Template cache HIT for %s (key=%s, hits=%d)",
                    doc_path, key, entry.hit_count,
                )
            return entry
    
    def put(
        self,
        doc_path: str,
        onset_page: int,
        routing_dict: dict,
        field_map_dicts: list[dict] | None = None,
        name_samples: list[str] | None = None,
    ) -> None:
        """Store template in cache.
        
        Parameters
        ----------
        routing_dict: serialized VisionRoutingResult (as dict)
        field_map_dicts: serialized FieldMapping list (as list of dicts)
        name_samples: PERSON name strings from vision analysis
        """
        key = self._compute_key(doc_path, onset_page)
        if key is None:
            return

        with self._lock:
            # Evict oldest if at capacity
            if len(self._cache) >= self._max_entries:
                oldest = min(self._cache, key=lambda k: self._cache[k].hit_count)
                del self._cache[oldest]

            self._cache[key] = CacheEntry(
                routing_dict=routing_dict,
                field_map_dicts=field_map_dicts,
                name_samples=name_samples or [],
            )

            logger.info(
                "Template cache STORE for %s (key=%s, total=%d)",
                doc_path, key, len(self._cache),
            )
    
    @property
    def size(self) -> int:
        return len(self._cache)
    
    def clear(self) -> None:
        self._cache.clear()
    
    def stats(self) -> dict:
        """Return cache statistics."""
        total_hits = sum(e.hit_count for e in self._cache.values())
        return {
            "entries": len(self._cache),
            "total_hits": total_hits,
            "max_entries": self._max_entries,
        }