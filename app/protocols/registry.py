"""Protocol registry — Phase 3.

Maintains a lookup table of available protocols (built-in + custom YAML)
keyed by ``protocol_id``.  The registry is initialised once at startup
and used by the notification pipeline to resolve the active protocol for
a given job.
"""
from __future__ import annotations

from app.protocols.loader import load_all_protocols
from app.protocols.protocol import Protocol


class ProtocolRegistry:
    """In-memory registry of available breach notification protocols."""

    def __init__(self, protocols: list[Protocol] | None = None) -> None:
        self._protocols: dict[str, Protocol] = {}
        if protocols is not None:
            for p in protocols:
                self._protocols[p.protocol_id] = p

    def register(self, protocol: Protocol) -> None:
        """Register (or replace) a protocol."""
        self._protocols[protocol.protocol_id] = protocol

    def get(self, protocol_id: str) -> Protocol:
        """Return the protocol with *protocol_id* or raise ``KeyError``."""
        try:
            return self._protocols[protocol_id]
        except KeyError:
            raise KeyError(f"Protocol not found: {protocol_id!r}")

    def list_all(self) -> list[Protocol]:
        """Return all registered protocols sorted by ``protocol_id``."""
        return sorted(self._protocols.values(), key=lambda p: p.protocol_id)

    @classmethod
    def default(cls) -> ProtocolRegistry:
        """Return a registry loaded from the default ``config/protocols/`` directory."""
        protocols = load_all_protocols("config/protocols")
        return cls(protocols)
