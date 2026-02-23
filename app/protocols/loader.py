"""Protocol YAML loader — Phase 3.

Loads protocol definitions from ``config/protocols/*.yaml`` files and
returns ``Protocol`` dataclass instances.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from app.protocols.protocol import Protocol

_REQUIRED_FIELDS: frozenset[str] = frozenset({
    "protocol_id",
    "name",
    "jurisdiction",
    "triggering_entity_types",
    "notification_threshold",
    "notification_deadline_days",
    "required_notification_content",
    "regulatory_framework",
})


def load_protocol(path: str | Path) -> Protocol:
    """Load a single protocol from a YAML file.

    Raises
    ------
    ValueError
        If any required field is missing from the YAML document.
    """
    path = Path(path)
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping, got {type(data).__name__}")

    missing = _REQUIRED_FIELDS - data.keys()
    if missing:
        raise ValueError(f"{path}: missing required fields: {sorted(missing)}")

    # Separate known optional fields from truly extra keys.
    known_optional = {"individual_deadline_days", "requires_hhs_notification"}
    extra_keys = data.keys() - _REQUIRED_FIELDS - known_optional
    extra = {k: data[k] for k in extra_keys}

    return Protocol(
        protocol_id=data["protocol_id"],
        name=data["name"],
        jurisdiction=data["jurisdiction"],
        triggering_entity_types=data["triggering_entity_types"],
        notification_threshold=int(data["notification_threshold"]),
        notification_deadline_days=int(data["notification_deadline_days"]),
        required_notification_content=data["required_notification_content"],
        regulatory_framework=data["regulatory_framework"],
        individual_deadline_days=data.get("individual_deadline_days"),
        requires_hhs_notification=bool(data.get("requires_hhs_notification", False)),
        extra=extra,
    )


def load_all_protocols(directory: str | Path = "config/protocols") -> list[Protocol]:
    """Load all ``*.yaml`` protocol files from *directory*.

    Raises
    ------
    ValueError
        If any YAML file fails validation.
    """
    directory = Path(directory)
    protocols: list[Protocol] = []
    for path in sorted(directory.iterdir()):
        if path.suffix not in (".yaml", ".yml"):
            continue
        protocols.append(load_protocol(path))
    return protocols
