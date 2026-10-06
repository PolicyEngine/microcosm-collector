"""Automated Alembic migration entry point for Cloud Run deployment."""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.reflection import Inspector

from telemetry_collector.database import Base, create_database_engine

SERVICE_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = SERVICE_ROOT / "alembic.ini"
MIGRATIONS = SERVICE_ROOT / "migrations"
MIGRATION_LOCK_NAME = "microcosm-telemetry-alembic"


class SchemaState(StrEnum):
    """Recognized database states before an automated migration."""

    EMPTY = "empty"
    LEGACY = "legacy"
    VERSIONED = "versioned"
    UNEXPECTED = "unexpected"


LEGACY_COLUMNS: dict[str, dict[str, tuple[type[sa.types.TypeEngine[Any]], bool]]] = {
    "telemetry_runs": {
        "run_id": (sa.Text, False),
        "country_code": (sa.Text, False),
        "pipeline": (sa.Text, False),
        "candidate_id": (sa.Text, True),
        "release_id": (sa.Text, True),
        "run_kind": (sa.Text, False),
        "owner_hf_id": (sa.Text, False),
        "owner_hf_username": (sa.Text, False),
        "status": (sa.Text, False),
        "current_stage": (sa.Text, False),
        "started_at": (sa.DateTime, False),
        "updated_at": (sa.DateTime, False),
        "ended_at": (sa.DateTime, True),
        "heartbeat_at": (sa.DateTime, True),
        "resources": (JSONB, True),
        "work": (JSONB, True),
        "failure": (JSONB, True),
    },
    "telemetry_events": {
        "event_id": (sa.Text, False),
        "run_id": (sa.Text, False),
        "producer_id": (sa.Text, False),
        "sequence": (sa.BigInteger, False),
        "emitted_at": (sa.DateTime, False),
        "received_at": (sa.DateTime, False),
        "event_type": (sa.Text, False),
        "stage_id": (sa.Text, True),
        "status": (sa.Text, False),
        "message": (sa.Text, True),
        "details": (JSONB, False),
        "resources": (JSONB, True),
    },
}


def _columns_match(inspector: Inspector, table_name: str) -> bool:
    expected = LEGACY_COLUMNS[table_name]
    actual = {column["name"]: column for column in inspector.get_columns(table_name)}
    if set(actual) != set(expected):
        return False
    return all(
        isinstance(actual[name]["type"], type_class)
        and actual[name]["nullable"] is nullable
        for name, (type_class, nullable) in expected.items()
    )


def classify_schema(inspector: Inspector) -> SchemaState:
    """Classify a database without changing it."""

    tables = set(inspector.get_table_names())
    if "alembic_version" in tables:
        return SchemaState.VERSIONED
    telemetry_tables = {table for table in tables if table.startswith("telemetry_")}
    if not telemetry_tables:
        return SchemaState.EMPTY
    if telemetry_tables != set(LEGACY_COLUMNS):
        return SchemaState.UNEXPECTED
    if not all(_columns_match(inspector, table) for table in LEGACY_COLUMNS):
        return SchemaState.UNEXPECTED

    run_primary_key = inspector.get_pk_constraint("telemetry_runs")
    event_primary_key = inspector.get_pk_constraint("telemetry_events")
    if run_primary_key.get("constrained_columns") != ["run_id"]:
        return SchemaState.UNEXPECTED
    if event_primary_key.get("constrained_columns") != ["event_id"]:
        return SchemaState.UNEXPECTED

    event_uniques = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("telemetry_events")
    }
    if ("run_id", "producer_id", "sequence") not in event_uniques:
        return SchemaState.UNEXPECTED
    event_foreign_keys = {
        (
            tuple(constraint["constrained_columns"]),
            constraint["referred_table"],
            tuple(constraint["referred_columns"]),
        )
        for constraint in inspector.get_foreign_keys("telemetry_events")
    }
    if (("run_id",), "telemetry_runs", ("run_id",)) not in event_foreign_keys:
        return SchemaState.UNEXPECTED

    run_indexes = {
        (index["name"], tuple(index["column_names"]))
        for index in inspector.get_indexes("telemetry_runs")
    }
    event_indexes = {
        (index["name"], tuple(index["column_names"]))
        for index in inspector.get_indexes("telemetry_events")
    }
    if (
        "telemetry_runs_country_updated_idx",
        ("country_code", "updated_at"),
    ) not in run_indexes:
        return SchemaState.UNEXPECTED
    if (
        "telemetry_events_run_order_idx",
        ("run_id", "emitted_at", "producer_id", "sequence"),
    ) not in event_indexes:
        return SchemaState.UNEXPECTED
    return SchemaState.LEGACY


def alembic_config() -> Config:
    """Return Alembic configuration independent of the current directory."""

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(MIGRATIONS))
    return config


def _verify_connection(connection: Connection, config: Config) -> None:
    expected_heads = set(ScriptDirectory.from_config(config).get_heads())
    current_heads = set(MigrationContext.configure(connection).get_current_heads())
    if not current_heads or current_heads != expected_heads:
        raise RuntimeError(
            "Database is not at the complete Alembic head after migration."
        )
    context = MigrationContext.configure(
        connection,
        opts={"compare_type": True, "compare_server_default": True},
    )
    differences = compare_metadata(context, Base.metadata)
    if differences:
        raise RuntimeError(
            "Database schema differs from SQLAlchemy metadata after migration "
            f"({len(differences)} difference(s))."
        )


def verify_database(database_url: str) -> None:
    """Require a reachable database at Alembic head with no ORM schema drift."""

    engine = create_database_engine(database_url)
    try:
        with engine.connect() as connection:
            _verify_connection(connection, alembic_config())
    finally:
        engine.dispose()


def upgrade_database(database_url: str) -> SchemaState:
    """Replace the unversioned prototype schema, then upgrade to Alembic head."""

    engine: Engine = create_database_engine(database_url)
    try:
        config = alembic_config()
        with engine.begin() as connection:
            connection.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtext(:name), 0)"),
                {"name": MIGRATION_LOCK_NAME},
            )
            state = classify_schema(inspect(connection))
            if state is SchemaState.UNEXPECTED:
                raise RuntimeError(
                    "Database schema does not match an empty, legacy, or "
                    "Alembic-managed state."
                )
            config.attributes["connection"] = connection
            if state is SchemaState.LEGACY:
                Base.metadata.drop_all(connection)
            command.upgrade(config, "head")
            _verify_connection(connection, config)
        return state
    finally:
        engine.dispose()


def database_revision_is_current(engine: Engine) -> bool:
    """Return whether the database is reachable and at the Alembic head."""

    config = alembic_config()
    expected = ScriptDirectory.from_config(config).get_current_head()
    with engine.connect() as connection:
        connection.execute(sa.select(1))
        current = MigrationContext.configure(connection).get_current_revision()
    return current is not None and current == expected


def main() -> None:
    """Upgrade the database named by DATABASE_URL."""

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required to run migrations.")
    upgrade_database(database_url)


if __name__ == "__main__":
    main()
