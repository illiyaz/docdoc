"""Error handling task: categorize failures, apply retry logic, and escalate.

Classifies pipeline exceptions into retryable and non-retryable categories.
Retryable failures are requeued with exponential back-off up to max_retries.
Non-retryable failures are written to IngestionRun.error_summary and routed
to the human review queue.

Error categories
----------------
TRANSIENT_IO     : network/disk errors; retryable
OCR_TIMEOUT      : PaddleOCR took too long; retryable
CORRUPT_FILE     : unrecoverable parse failure; non-retryable
SCHEMA_VIOLATION : output doesn't match ExtractedBlock contract; non-retryable
UNKNOWN          : uncategorized; retryable once then escalated
"""
from __future__ import annotations

from enum import Enum


class ErrorCategory(str, Enum):
    TRANSIENT_IO = "transient_io"
    OCR_TIMEOUT = "ocr_timeout"
    CORRUPT_FILE = "corrupt_file"
    SCHEMA_VIOLATION = "schema_violation"
    UNKNOWN = "unknown"


class ErrorHandlerTask:
    """Prefect task: categorize and route pipeline failures."""

    def run(self, error: Exception, document_id: str, run_id: str) -> ErrorCategory:
        """Classify the error, apply retry or escalation, return the category."""
        ...

    def categorize(self, error: Exception) -> ErrorCategory:
        """Map an exception to its ErrorCategory."""
        ...

    def should_retry(self, category: ErrorCategory, attempt: int) -> bool:
        """Return True if the category is retryable and within max_retries."""
        ...
