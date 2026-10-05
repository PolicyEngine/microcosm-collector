"""Automated Alembic migration entry point for Cloud Run deployment."""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.engine.reflection import Inspector

from telemetry_collector.database import create_database_engine

SERVICE_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = SERVICE_ROOT / "alembic.ini"
MIGRATIONS = SERVICE_ROOT / "migrations"


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
    telemetry_tables = tables & set(LEGACY_COLUMNS)
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


def upgrade_database(database_url: str) -> SchemaState:
    """Adopt a recognized legacy database and upgrade it to Alembic head."""

    engine: Engine = create_database_engine(database_url)
    try:
        state = classify_schema(inspect(engine))
        if state is SchemaState.UNEXPECTED:
            raise RuntimeError(
                "Database schema does not match an empty, legacy, or Alembic-managed state."
            )
        config = alembic_config()
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            if state is SchemaState.LEGACY:
                command.stamp(config, "0001_legacy_schema")
            command.upgrade(config, "head")
        return state
    finally:
        engine.dispose()


def main() -> None:
    """Upgrade the database named by DATABASE_URL."""

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required to run migrations.")
    upgrade_database(database_url)


if __name__ == "__main__":
    main()
