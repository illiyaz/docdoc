from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from app.core.security import SecurityService


class StorageMode(StrEnum):
    STRICT = "strict"
    INVESTIGATION = "investigation"


@dataclass(slots=True)
class StoragePolicyConfig:
    mode: StorageMode
    mask_normalized_in_strict: bool = True
    investigation_retention_days: int = 30


def _mask_value(value: str) -> str:
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def build_extraction_storage(
    *,
    raw_value: str,
    normalized_value: str | None,
    tenant_salt: str,
    security: SecurityService,
    config: StoragePolicyConfig,
    now: datetime | None = None,
) -> dict[str, object | None]:
    if not raw_value:
        raise ValueError("raw_value is required")

    normalized = normalized_value or raw_value
    hashed_value = security.hash_with_tenant_salt(normalized, tenant_salt)

    if config.mode == StorageMode.STRICT:
        return {
            "normalized_value": _mask_value(normalized) if config.mask_normalized_in_strict else normalized,
            "hashed_value": hashed_value,
            "raw_value_encrypted": None,
            "storage_policy": "hash",
            "retention_until": None,
        }

    if config.mode == StorageMode.INVESTIGATION:
        current = now or datetime.now(timezone.utc)
        retention_until = current + timedelta(days=config.investigation_retention_days)
        encrypted = security.encrypt(raw_value)
        return {
            "normalized_value": normalized,
            "hashed_value": hashed_value,
            "raw_value_encrypted": encrypted,
            "storage_policy": "encrypted",
            "retention_until": retention_until,
        }

    raise ValueError(f"Unsupported storage mode: {config.mode}")
