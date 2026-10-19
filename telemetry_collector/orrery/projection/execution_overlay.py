"""The execution overlay as its own file, shared by evidence capture and projection.

:func:`microcosm.graph.evidence.collect_execution_evidence` verifies recorded
runs against the content store and returns the overlay;
:mod:`microcosm.graph.orrery` projects it. The file holds that same object as
canonical JSON, so a process that only projects can read it back. This module
needs only the standard library, so that process needs neither the store nor
numpy, pandas or microcosm-frame.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .canonical import canonical_json
from .presentation import require
from .schema import _MAX_BYTES as _MAX_OVERLAY_BYTES
from .schema import _plain_json

__all__ = ["EXECUTION_PROTOCOL", "execution_overlay_bytes", "load_execution_overlay"]

EXECUTION_PROTOCOL = "microcosm.graph.execution-evidence.v1"


def _overlay_object(value: object) -> dict:
    require(
        type(value) is dict and value.get("protocol") == EXECUTION_PROTOCOL,
        f"execution overlay must be a {EXECUTION_PROTOCOL} object",
    )
    return value  # type: ignore[return-value]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key in execution overlay")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("Graph evidence: execution overlay requires finite JSON numbers.")


def execution_overlay_bytes(overlay: object) -> bytes:
    """Serialize an overlay from ``collect_execution_evidence`` as canonical JSON.

    :func:`load_execution_overlay` reads the bytes back as an equal object, so
    projecting it gives the same Orrery JSON as projecting ``overlay`` itself.
    """

    return canonical_json(_overlay_object(_plain_json(overlay)))


def load_execution_overlay(path: Path) -> dict:
    """Read an overlay file written from :func:`execution_overlay_bytes`.

    Only a UTF-8 regular file within the schema's serialized size limit, which
    evidence collection and projection both apply to the overlay, is read.
    Duplicate keys, non-finite numbers, values outside the schema's JSON bounds
    and any other protocol are refused. The overlay's graph binding and its
    records are checked when it is projected.
    """

    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode), "execution overlay must be a regular file")
        require(info.st_size <= _MAX_OVERLAY_BYTES, "execution overlay byte limit")
        payload = stream.read(_MAX_OVERLAY_BYTES + 1)
    require(len(payload) <= _MAX_OVERLAY_BYTES, "execution overlay byte limit")
    value = json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )
    return _overlay_object(_plain_json(value))
