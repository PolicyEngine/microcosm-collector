"""Canonical identity bytes for graph declarations and manifests."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from .decl import DESCRIPTIVE_FIELDS

__all__ = ["canonical_json", "normative", "sha256", "sha256_domain"]


def _json_value(value: object) -> object:
    """Return ``value`` using only the graph's closed JSON value grammar."""

    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numbers are not canonical JSON")
        return value
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, Mapping):
        converted: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical mappings require string keys")
            converted[key] = _json_value(child)
        return converted
    if isinstance(value, list | tuple):
        return [_json_value(child) for child in value]
    raise TypeError(
        f"{type(value).__name__} is not part of the canonical JSON value grammar"
    )


def canonical_json(obj: object) -> bytes:
    """Encode a value as deterministic, whitespace-free UTF-8 JSON.

    Maps have string keys and are ordered lexicographically.  Tuples use the
    JSON array representation, enums use their values, and floats use the
    standard library's shortest round-trip representation.  Unsupported
    objects are rejected instead of being converted with ``str``.
    """

    return json.dumps(
        _json_value(obj),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256(payload: bytes) -> str:
    """Return the lowercase hexadecimal SHA-256 digest of ``payload``."""

    return hashlib.sha256(payload).hexdigest()


def sha256_domain(domain: str, payload: bytes) -> str:
    """Hash ``payload`` after a UTF-8 domain and a NUL separator."""

    if not isinstance(domain, str):
        raise TypeError("hash domain must be a string")
    if "\0" in domain:
        raise ValueError("hash domain must not contain NUL")
    if not isinstance(payload, bytes):
        raise TypeError("hash payload must be bytes")
    return hashlib.sha256(domain.encode("utf-8") + b"\0" + payload).hexdigest()


def _declaration_value(value: object) -> object:
    """Project nested declaration dataclasses without descriptive fields."""

    if isinstance(value, Enum):
        return _declaration_value(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _declaration_value(getattr(value, item.name))
            for item in fields(value)
            if item.name not in DESCRIPTIVE_FIELDS
        }
    if isinstance(value, Mapping):
        # Mapping keys are data, not declaration field names.  A normative
        # parameter named ``description`` must therefore remain normative.
        return {key: _declaration_value(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return tuple(_declaration_value(child) for child in value)
    if isinstance(value, list):
        return [_declaration_value(child) for child in value]
    return value


def normative(node: Any) -> object:
    """Return a declaration's recursive normative projection.

    :class:`~microcosm.graph.decl.Node` supplies the authoritative top-level
    projection through ``Node.normative()``.  The recursive walk converts its
    nested declarations and also makes the helper useful for a nested
    declaration such as ``SourceRef`` in isolation.
    """

    project = getattr(node, "normative", None)
    root = project() if callable(project) else node
    return _declaration_value(root)
