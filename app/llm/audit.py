"""LLM call auditing -- records every LLM invocation for governance.

Every call to the Ollama backend is persisted in the ``llm_call_logs`` table
so that audit reviewers can trace which decisions were LLM-assisted, what
the model saw, and what it returned.

.. important::
   ``prompt_text`` stored here must NEVER contain raw PII.  Callers are
   responsible for masking / redacting before passing values in.
"""
from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import LLMCallLog

# Simple regex patterns for common raw PII that should NOT appear in prompts
_PII_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),         # US SSN
    re.compile(r"\b\d{9}\b"),                        # 9-digit number (potential SSN)
    re.compile(r"\b\d{16}\b"),                       # 16-digit number (potential credit card)
    re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"),  # credit card with separators
]


def _contains_potential_pii(text: str) -> bool:
    """Check if text contains patterns that look like raw PII.

    This is a safety net -- callers should already be masking PII.
    """
    for pattern in _PII_PATTERNS:
        if pattern.search(text):
            return True
    return False


def log_llm_call(
    db_session: Session,
    *,
    document_id: UUID | None = None,
    use_case: str,
    model: str,
    prompt_text: str,
    response_text: str,
    decision: str | None = None,
    accepted: bool | None = None,
    latency_ms: int | None = None,
    token_count: int | None = None,
) -> LLMCallLog:
    """Create an ``LLMCallLog`` row in the database.

    Parameters
    ----------
    db_session:
        Active SQLAlchemy session.
    document_id:
        Optional FK to the document being processed.
    use_case:
        Short label describing the LLM use-case (e.g.
        ``"classify_ambiguous_entity"``).
    model:
        Model identifier string (e.g. ``"qwen2.5:7b"``).
    prompt_text:
        The full prompt sent to the model.  **Must not contain raw PII.**
    response_text:
        The raw response from the model.
    decision:
        Summary of the LLM's decision, if applicable.
    accepted:
        Whether the deterministic pipeline accepted the LLM's suggestion.
    latency_ms:
        Round-trip latency in milliseconds.
    token_count:
        Number of tokens in the response, if known.

    Returns
    -------
    LLMCallLog
        The newly created row (already added to the session).
    """
    # Safety check -- warn but do not block (the caller may have a valid pattern)
    if _contains_potential_pii(prompt_text):
        import logging

        logging.getLogger(__name__).warning(
            "Potential raw PII detected in LLM prompt text (use_case=%s). "
            "Callers must mask PII before logging.",
            use_case,
        )

    row = LLMCallLog(
        document_id=document_id,
        use_case=use_case,
        model=model,
        prompt_text=prompt_text,
        response_text=response_text,
        decision=decision,
        accepted=accepted,
        latency_ms=latency_ms,
        token_count=token_count,
    )
    db_session.add(row)
    db_session.flush()
    return row


def get_llm_calls(
    db_session: Session,
    *,
    document_id: UUID | None = None,
    use_case: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Query ``llm_call_logs`` with optional filters.

    Parameters
    ----------
    db_session:
        Active SQLAlchemy session.
    document_id:
        Filter by document FK (exact match).
    use_case:
        Filter by use-case label (exact match).
    limit:
        Maximum number of rows to return (default 100).

    Returns
    -------
    list[dict]
        Each dict has keys matching the ``LLMCallLog`` columns.
    """
    stmt = select(LLMCallLog).order_by(LLMCallLog.created_at.desc())

    if document_id is not None:
        stmt = stmt.where(LLMCallLog.document_id == document_id)
    if use_case is not None:
        stmt = stmt.where(LLMCallLog.use_case == use_case)

    stmt = stmt.limit(limit)

    rows = db_session.execute(stmt).scalars().all()
    return [
        {
            "id": str(row.id),
            "document_id": str(row.document_id) if row.document_id else None,
            "use_case": row.use_case,
            "model": row.model,
            "prompt_text": row.prompt_text,
            "response_text": row.response_text,
            "decision": row.decision,
            "accepted": row.accepted,
            "latency_ms": row.latency_ms,
            "token_count": row.token_count,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]
