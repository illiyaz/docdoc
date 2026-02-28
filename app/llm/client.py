"""Ollama LLM client wrapper -- governance-gated, audit-logged.

Wraps the Ollama REST API (``POST /api/generate``) with:

- **Governance gate**: every call checks ``settings.llm_assist_enabled``.
  When ``False`` (the default), ``generate()`` raises ``LLMDisabledError``.
- **Full audit logging**: every call is recorded in the ``llm_call_logs``
  table via :func:`app.llm.audit.log_llm_call`.
- **Latency tracking**: wall-clock time is measured per request.
- **PII safety check**: a lightweight regex scan warns if the prompt
  appears to contain raw PII (SSN, credit card patterns).

The client uses ``httpx`` for synchronous HTTP calls (no async needed -- the
pipeline is Prefect-orchestrated, not async FastAPI).

Ollama is expected to run locally on the same machine or LAN -- this satisfies
the air-gap deployment requirement.
"""
from __future__ import annotations

import re
import time

import httpx
from sqlalchemy.orm import Session

from app.core.settings import get_settings
from app.llm.audit import log_llm_call

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class LLMDisabledError(RuntimeError):
    """Raised when ``llm_assist_enabled`` is ``False``."""


class LLMConnectionError(ConnectionError):
    """Raised when Ollama is unreachable."""


class LLMTimeoutError(TimeoutError):
    """Raised when the Ollama request exceeds the configured timeout."""


# ---------------------------------------------------------------------------
# Safety patterns -- simple regex that should NOT appear in prompts
# ---------------------------------------------------------------------------

_PII_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),         # US SSN
    re.compile(r"\b\d{9}\b"),                        # 9-digit number
    re.compile(r"\b\d{16}\b"),                       # 16-digit number (CC)
    re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"),  # CC with separators
]


def _prompt_contains_potential_pii(text: str) -> bool:
    """Return ``True`` if the prompt text matches common raw PII patterns."""
    for pattern in _PII_PATTERNS:
        if pattern.search(text):
            return True
    return False


# ---------------------------------------------------------------------------
# OllamaClient
# ---------------------------------------------------------------------------


class OllamaClient:
    """Synchronous client for the local Ollama REST API.

    Parameters
    ----------
    base_url:
        Ollama base URL.  Defaults to ``settings.ollama_url``.
    model:
        Model tag (e.g. ``"qwen2.5:7b"``).  Defaults to
        ``settings.ollama_model``.
    timeout_s:
        Request timeout in seconds.  Defaults to
        ``settings.ollama_timeout_s``.
    db_session:
        Optional SQLAlchemy session for audit logging.  When ``None``,
        LLM calls are NOT logged (useful for health checks).
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        timeout_s: int | None = None,
        db_session: Session | None = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.ollama_url).rstrip("/")
        self.model = model or settings.ollama_model
        self.timeout_s = timeout_s if timeout_s is not None else settings.ollama_timeout_s
        self.db_session = db_session
        self._last_latency_ms: int | None = None

    # -- public API ---------------------------------------------------------

    def generate(
        self,
        prompt: str,
        system: str | None = None,
        *,
        use_case: str = "general",
        document_id=None,
    ) -> str:
        """Send a prompt to Ollama and return the generated text.

        Parameters
        ----------
        prompt:
            The user prompt.  **Must not contain raw PII.**
        system:
            Optional system prompt.
        use_case:
            Label for audit logging (e.g. ``"classify_ambiguous_entity"``).
        document_id:
            Optional document FK for audit logging.

        Returns
        -------
        str
            The model's response text.

        Raises
        ------
        LLMDisabledError
            If ``llm_assist_enabled`` is ``False``.
        LLMConnectionError
            If Ollama is unreachable.
        LLMTimeoutError
            If the request exceeds the configured timeout.
        """
        # Governance gate
        settings = get_settings()
        if not settings.llm_assist_enabled:
            raise LLMDisabledError(
                "LLM assist is disabled (LLM_ASSIST_ENABLED=false). "
                "Enable it in settings to use LLM features."
            )

        # PII safety check
        if _prompt_contains_potential_pii(prompt):
            import logging

            logging.getLogger(__name__).warning(
                "Potential raw PII detected in LLM prompt (use_case=%s). "
                "Callers must mask PII before sending to the LLM.",
                use_case,
            )

        # Build request payload
        payload: dict = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
        }
        if system is not None:
            payload["system"] = system

        # Execute with timing
        start = time.monotonic()
        try:
            response = httpx.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=self.timeout_s,
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            self._last_latency_ms = elapsed_ms
            raise LLMTimeoutError(
                f"Ollama request timed out after {self.timeout_s}s"
            ) from exc
        except httpx.ConnectError as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            self._last_latency_ms = elapsed_ms
            raise LLMConnectionError(
                f"Cannot connect to Ollama at {self.base_url}"
            ) from exc
        except httpx.HTTPError as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            self._last_latency_ms = elapsed_ms
            raise LLMConnectionError(
                f"Ollama HTTP error: {exc}"
            ) from exc

        elapsed_ms = int((time.monotonic() - start) * 1000)
        self._last_latency_ms = elapsed_ms

        data = response.json()
        response_text = data.get("response", "")
        token_count = data.get("eval_count")

        # Audit log
        if self.db_session is not None:
            log_llm_call(
                self.db_session,
                document_id=document_id,
                use_case=use_case,
                model=self.model,
                prompt_text=prompt,
                response_text=response_text,
                latency_ms=elapsed_ms,
                token_count=token_count,
            )

        return response_text

    def is_available(self) -> bool:
        """Check whether Ollama is reachable.

        Returns ``False`` if the server is not running or unreachable.
        Never raises an exception.
        """
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    @property
    def last_latency_ms(self) -> int | None:
        """Wall-clock latency of the most recent ``generate()`` call (ms)."""
        return self._last_latency_ms
