"""Response middleware: block outgoing JSON that contains raw PII values.

This is a last-resort safety net.  All application code must already
avoid placing raw PII in responses.  If any PII pattern from
app.core.logging.PII_PATTERNS fires on a JSON response body, the
response is replaced with HTTP 500 and the incident is logged.

Rationale
---------
CLAUDE.md § 9: "Never send raw PII to the frontend API — mask at the
API layer before response."  This middleware enforces that rule even
when application code has a bug.

Safety note: the matched text span is never logged — only the entity
pattern index is recorded to avoid leaking PII into log output.
"""
from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.logging import PII_PATTERNS

logger = logging.getLogger(__name__)

# Headers that must not be copied verbatim because their values become
# invalid once we re-buffer the body into a new Response.
_SKIP_HEADERS = frozenset({"content-length", "transfer-encoding"})


class PIIFilterMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that blocks JSON responses containing raw PII.

    Only responses with Content-Type: application/json are scanned.
    Other content types (HTML, binary, etc.) are passed through unchanged.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        # Skip PII scanning when masking is disabled (MVP testing mode)
        from app.core.settings import get_settings
        if not get_settings().pii_masking_enabled:
            return response

        # Only scan JSON payloads — streaming responses (SSE, file
        # downloads, etc.) must pass through without buffering.
        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type:
            return response

        # Buffer the full response body (JSON responses are always small)
        chunks: list[bytes] = []
        async for chunk in response.body_iterator:
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            chunks.append(chunk)
        body = b"".join(chunks)
        text = body.decode("utf-8", errors="replace")

        # Scan every PII pattern — stop on first hit
        for idx, pattern in enumerate(PII_PATTERNS):
            if pattern.search(text):
                # SAFETY: log the pattern index only — never the matched text
                logger.error(
                    "PIIFilterMiddleware: raw PII detected in response body "
                    "(pattern_index=%d, path=%s). Response blocked.",
                    idx,
                    request.url.path,
                )
                return JSONResponse(
                    status_code=500,
                    content={"detail": "Internal error: response blocked by PII filter."},
                )

        # Clean response — reconstruct from the buffered body
        safe_headers = {
            k: v for k, v in response.headers.items()
            if k.lower() not in _SKIP_HEADERS
        }
        return Response(
            content=body,
            status_code=response.status_code,
            headers=safe_headers,
            media_type=response.media_type,
        )
