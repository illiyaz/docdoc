from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from cryptography.fernet import Fernet


class EncryptionProvider(Protocol):
    def encrypt(self, value: str) -> str:
        ...

    def decrypt(self, token: str) -> str:
        ...


class FernetEncryptionProvider:
    def __init__(self, key: str):
        self._fernet = Fernet(key.encode("utf-8"))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")


@dataclass(slots=True)
class SecurityService:
    encryption_provider: EncryptionProvider | None = None

    def hash_with_tenant_salt(self, value: str, tenant_salt: str) -> str:
        payload = f"{tenant_salt}:{value}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def encrypt(self, value: str) -> str:
        if self.encryption_provider is None:
            raise ValueError("Encryption provider is required for this operation")
        return self.encryption_provider.encrypt(value)

    def decrypt(self, token: str) -> str:
        if self.encryption_provider is None:
            raise ValueError("Encryption provider is required for this operation")
        return self.encryption_provider.decrypt(token)
