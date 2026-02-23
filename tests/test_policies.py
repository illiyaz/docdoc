"""Tests for app/core/policies.py — storage policy behaviour.

Covers:
- STRICT mode invariants (raw_value_encrypted IS NULL, hashed_value set, retention None)
- INVESTIGATION mode invariants (encrypted, retention set, fail-closed without key)
- Hash correctness (SHA-256, 64-char hex, deterministic, tenant-isolated)
- Normalised-value masking in STRICT mode
- Empty raw_value rejection
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from app.core.policies import StorageMode, StoragePolicyConfig, build_extraction_storage
from app.core.security import FernetEncryptionProvider, SecurityService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RAW = "john.doe@example.com"
SALT = "tenant-1"


def _strict_security() -> SecurityService:
    """SecurityService with a Fernet key (key presence irrelevant for STRICT)."""
    return SecurityService(FernetEncryptionProvider(Fernet.generate_key().decode()))


def _investigation_security(key: str | None = None) -> SecurityService:
    key = key or Fernet.generate_key().decode()
    return SecurityService(FernetEncryptionProvider(key))


def _strict_payload(raw: str = RAW, salt: str = SALT, **kw) -> dict:
    return build_extraction_storage(
        raw_value=raw,
        normalized_value=raw,
        tenant_salt=salt,
        security=_strict_security(),
        config=StoragePolicyConfig(mode=StorageMode.STRICT, **kw),
    )


# ---------------------------------------------------------------------------
# STRICT mode invariants
# ---------------------------------------------------------------------------

def test_strict_mode_never_stores_encrypted_raw_value():
    payload = _strict_payload()
    assert payload["raw_value_encrypted"] is None


def test_strict_mode_storage_policy_is_hash():
    assert _strict_payload()["storage_policy"] == "hash"


def test_strict_mode_retention_until_is_none():
    assert _strict_payload()["retention_until"] is None


def test_strict_mode_hashed_value_is_populated():
    h = _strict_payload()["hashed_value"]
    assert isinstance(h, str) and len(h) > 0


def test_strict_mode_normalized_value_is_masked_by_default():
    payload = _strict_payload(mask_normalized_in_strict=True)
    assert payload["normalized_value"] != RAW
    assert "*" in payload["normalized_value"]


def test_strict_mode_normalized_value_unmasked_when_opt_out():
    payload = _strict_payload(mask_normalized_in_strict=False)
    assert payload["normalized_value"] == RAW


# ---------------------------------------------------------------------------
# Hash correctness
# ---------------------------------------------------------------------------

def test_hashed_value_is_64_char_hex():
    h = _strict_payload()["hashed_value"]
    assert len(h) == 64
    int(h, 16)  # raises if not valid hex


def test_hash_matches_sha256_of_salt_colon_value():
    expected = hashlib.sha256(f"{SALT}:{RAW}".encode()).hexdigest()
    assert _strict_payload()["hashed_value"] == expected


def test_hash_is_deterministic():
    p1 = _strict_payload()
    p2 = _strict_payload()
    assert p1["hashed_value"] == p2["hashed_value"]


def test_hash_is_tenant_isolated():
    h1 = _strict_payload(salt="tenant-a")["hashed_value"]
    h2 = _strict_payload(salt="tenant-b")["hashed_value"]
    assert h1 != h2


def test_hash_differs_for_different_values():
    h1 = _strict_payload(raw="alice@example.com")["hashed_value"]
    h2 = _strict_payload(raw="bob@example.com")["hashed_value"]
    assert h1 != h2


# ---------------------------------------------------------------------------
# INVESTIGATION mode invariants
# ---------------------------------------------------------------------------

def test_investigation_mode_encrypts_raw_value_and_sets_retention():
    key = Fernet.generate_key().decode()
    security = _investigation_security(key)
    now = datetime(2026, 2, 8, tzinfo=timezone.utc)

    payload = build_extraction_storage(
        raw_value=RAW,
        normalized_value=RAW,
        tenant_salt=SALT,
        security=security,
        config=StoragePolicyConfig(mode=StorageMode.INVESTIGATION, investigation_retention_days=14),
        now=now,
    )

    encrypted = payload["raw_value_encrypted"]
    assert isinstance(encrypted, str)
    assert encrypted != RAW
    assert security.decrypt(encrypted) == RAW
    assert payload["retention_until"] == now + timedelta(days=14)
    assert payload["storage_policy"] == "encrypted"


def test_investigation_mode_hashed_value_is_populated():
    security = _investigation_security()
    payload = build_extraction_storage(
        raw_value=RAW,
        normalized_value=RAW,
        tenant_salt=SALT,
        security=security,
        config=StoragePolicyConfig(mode=StorageMode.INVESTIGATION),
    )
    h = payload["hashed_value"]
    assert isinstance(h, str) and len(h) == 64


def test_investigation_mode_retention_until_uses_config_days():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    payload = build_extraction_storage(
        raw_value=RAW,
        normalized_value=RAW,
        tenant_salt=SALT,
        security=_investigation_security(),
        config=StoragePolicyConfig(mode=StorageMode.INVESTIGATION, investigation_retention_days=90),
        now=now,
    )
    assert payload["retention_until"] == now + timedelta(days=90)


def test_investigation_mode_fail_closed_without_encryption_provider():
    """SecurityService with no provider must raise, never fall back to plaintext."""
    security = SecurityService(encryption_provider=None)
    with pytest.raises(ValueError):
        build_extraction_storage(
            raw_value=RAW,
            normalized_value=RAW,
            tenant_salt=SALT,
            security=security,
            config=StoragePolicyConfig(mode=StorageMode.INVESTIGATION),
        )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def test_empty_raw_value_raises_value_error():
    with pytest.raises(ValueError):
        build_extraction_storage(
            raw_value="",
            normalized_value=None,
            tenant_salt=SALT,
            security=_strict_security(),
            config=StoragePolicyConfig(mode=StorageMode.STRICT),
        )
