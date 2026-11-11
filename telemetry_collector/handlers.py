"""The worker's one registry of handlers, from the worker job's settings.

Every kind starts with the handler that keeps its files in intake storage
(``DEFAULT_HANDLERS``). Each handler module then registers its own handler
where the worker job has the setting it needs:

- ``calibration_trees``: ``calibration-diagnostics``, where
  ``COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN`` is set;
- ``orrery_graphs``: ``orrery-inputs``, where ``COLLECTOR_ORRERY_BUCKET`` is
  set.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

import sqlalchemy as sa

from telemetry_collector import calibration_trees, orrery_graphs
from telemetry_collector.worker import DEFAULT_HANDLERS, JobHandler


def handlers_from_environment(
    *, engine: sa.Engine | None, environ: Mapping[str, str] = os.environ
) -> Mapping[str, JobHandler]:
    """The handler for each kind, given the worker job's settings."""

    return {
        **DEFAULT_HANDLERS,
        **calibration_trees.registered_handlers(engine=engine, environ=environ),
        **orrery_graphs.registered_handlers(engine=engine, environ=environ),
    }
