from __future__ import annotations

import ast
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.pool import QueuePool

from telemetry_collector.database import (
    DB_MAX_OVERFLOW,
    DB_POOL_SIZE,
    Base,
    TelemetryEvent,
    TelemetryProducer,
    TelemetryRun,
    create_database_engine,
    normalize_database_url,
)


def test_orm_metadata_defines_runs_producers_and_events() -> None:
    assert set(Base.metadata.tables) == {
        "telemetry_runs",
        "telemetry_producers",
        "telemetry_events",
    }
    assert set(inspect(TelemetryRun).relationships.keys()) == {"producers"}
    assert set(inspect(TelemetryProducer).relationships.keys()) == {"run", "events"}
    assert set(inspect(TelemetryEvent).relationships.keys()) == {"producer"}


def test_events_are_bound_to_a_producer_and_sequence() -> None:
    event_table = TelemetryEvent.__table__
    foreign_keys = {
        (tuple(constraint.column_keys), constraint.referred_table.name)
        for constraint in event_table.foreign_key_constraints
    }
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in event_table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }

    assert (("run_id", "producer_id"), "telemetry_producers") in foreign_keys
    assert ("run_id", "producer_id", "sequence") in unique_columns


def test_database_url_selects_the_synchronous_psycopg_dialect() -> None:
    normalized = normalize_database_url(
        "postgresql://collector:secret@database.example/telemetry"
    )

    assert normalized.drivername == "postgresql+psycopg"
    assert normalized.password == "secret"


def test_engine_uses_sqlalchemy_queue_pool() -> None:
    engine = create_database_engine(
        "postgresql://collector:secret@database.example/telemetry"
    )
    try:
        assert isinstance(engine.pool, QueuePool)
        assert engine.pool.size() == DB_POOL_SIZE
        assert engine.pool._max_overflow == DB_MAX_OVERFLOW
    finally:
        engine.dispose()


def test_application_modules_do_not_import_psycopg_directly() -> None:
    package = Path(__file__).resolve().parents[1] / "telemetry_collector"
    imported_modules: set[str] = set()
    for source in package.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)

    assert not any(
        module == "psycopg" or module.startswith("psycopg.")
        for module in imported_modules
    )
