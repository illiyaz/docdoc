"""Email normalizer — Phase 2.

Converts a raw email address to a lowercase, whitespace-stripped
canonical form.  Gmail dot-normalization is applied: dots in the local
part of ``@gmail.com`` and ``@googlemail.com`` addresses are removed
because Gmail treats ``j.o.h.n@gmail.com`` and ``john@gmail.com`` as
identical mailboxes.

Sub-address tags (``user+tag@domain``) are preserved — stripping them
is a lossy transformation not appropriate here.

Safety rule: raw values are never logged.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Domains where dots in the local part are insignificant
_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def normalize_email(raw: str) -> str:
    """Return *raw* email address in canonical lowercase form.

    Parameters
    ----------
    raw:
        Raw email string extracted from source text.

    Returns
    -------
    str
        Lowercased, whitespace-stripped address.  For Gmail/Googlemail
        addresses, dots are removed from the local part.  For all other
        domains the address is returned as-is after lowercasing and
        stripping.  An empty or whitespace-only input is returned as
        ``""``.
    """
    stripped = raw.strip().lower()
    if not stripped:
        return ""

    if "@" not in stripped:
        # Not a valid email shape — return lowercased value without mutating
        logger.debug("normalize_email: no '@' found (length=%d)", len(stripped))
        return stripped

    local, _, domain = stripped.partition("@")

    if domain in _GMAIL_DOMAINS:
        local = local.replace(".", "")

    return f"{local}@{domain}"
