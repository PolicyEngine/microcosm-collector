"""Microcosm's Orrery projection, copied into the collector.

A Microcosm build saves its compiler schema (``graph.schema.json``) and its
execution overlay (``graph.execution.json``). :func:`orrery_json_from_schema`
turns those two inputs into the same Orrery JSON, byte for byte, that
Microcosm's own projection writes from them; ``tests/test_orrery_projection.py``
checks this against output saved from Microcosm.

The modules keep their Microcosm names and contents, so a copy from a later
Microcosm commit can be compared file by file. ``orrery.py`` leaves out
Microcosm's command-line entry point, whose overlay option reads Microcosm's
content store, and the two functions that compile a ``Graph`` object before
projecting it; every other module is unchanged. The modules need only the
standard library: not the content store, numpy, pandas or microcosm-frame.
"""

from .execution_overlay import (
    EXECUTION_PROTOCOL,
    execution_overlay_bytes,
    load_execution_overlay,
)
from .orrery import orrery_document_from_schema, orrery_json_from_schema

__all__ = [
    "EXECUTION_PROTOCOL",
    "execution_overlay_bytes",
    "load_execution_overlay",
    "orrery_document_from_schema",
    "orrery_json_from_schema",
]
