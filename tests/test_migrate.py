from __future__ import annotations

from unittest.mock import Mock

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from telemetry_collector.migrate import SchemaState, classify_schema


def _legacy_inspector() -> Mock:
    inspector = Mock()
    inspector.get_table_names.return_value = ["telemetry_runs", "telemetry_events"]
    column_types = {
        "telemetry_runs": {
            "run_id": sa.Text(),
            "country_code": sa.Text(),
            "pipeline": sa.Text(),
            "candidate_id": sa.Text(),
            "release_id": sa.Text(),
            "run_kind": sa.Text(),
            "owner_hf_id": sa.Text(),
            "owner_hf_username": sa.Text(),
            "status": sa.Text(),
            "current_stage": sa.Text(),
            "started_at": sa.DateTime(timezone=True),
            "updated_at": sa.DateTime(timezone=True),
            "ended_at": sa.DateTime(timezone=True),
            "heartbeat_at": sa.DateTime(timezone=True),
            "resources": JSONB(),
            "work": JSONB(),
            "failure": JSONB(),
        },
        "telemetry_events": {
            "event_id": sa.Text(),
            "run_id": sa.Text(),
            "producer_id": sa.Text(),
            "sequence": sa.BigInteger(),
            "emitted_at": sa.DateTime(timezone=True),
            "received_at": sa.DateTime(timezone=True),
            "event_type": sa.Text(),
            "stage_id": sa.Text(),
            "status": sa.Text(),
            "message": sa.Text(),
            "details": JSONB(),
            "resources": JSONB(),
        },
    }
    nullable = {
        "candidate_id",
        "release_id",
        "ended_at",
        "heartbeat_at",
        "resources",
        "work",
        "failure",
        "stage_id",
        "message",
    }
    inspector.get_columns.side_effect = lambda table: [
        {"name": name, "type": type_, "nullable": name in nullable}
        for name, type_ in column_types[table].items()
    ]
    inspector.get_pk_constraint.side_effect = lambda table: {
        "constrained_columns": ["run_id"] if table == "telemetry_runs" else ["event_id"]
    }
    inspector.get_unique_constraints.return_value = [
        {"column_names": ["run_id", "producer_id", "sequence"]}
    ]
    inspector.get_foreign_keys.return_value = [
        {
            "constrained_columns": ["run_id"],
            "referred_table": "telemetry_runs",
            "referred_columns": ["run_id"],
        }
    ]
    inspector.get_indexes.side_effect = lambda table: (
        [
            {
                "name": "telemetry_runs_country_updated_idx",
                "column_names": ["country_code", "updated_at"],
            }
        ]
        if table == "telemetry_runs"
        else [
            {
                "name": "telemetry_events_run_order_idx",
                "column_names": ["run_id", "emitted_at", "producer_id", "sequence"],
            }
        ]
    )
    return inspector


def test_classifies_empty_database() -> None:
    inspector = Mock()
    inspector.get_table_names.return_value = []

    assert classify_schema(inspector) is SchemaState.EMPTY


def test_classifies_alembic_database() -> None:
    inspector = Mock()
    inspector.get_table_names.return_value = ["alembic_version", "telemetry_runs"]

    assert classify_schema(inspector) is SchemaState.VERSIONED


def test_classifies_exact_legacy_database() -> None:
    assert classify_schema(_legacy_inspector()) is SchemaState.LEGACY


def test_rejects_partial_legacy_database() -> None:
    inspector = _legacy_inspector()
    inspector.get_table_names.return_value = ["telemetry_runs"]

    assert classify_schema(inspector) is SchemaState.UNEXPECTED


def test_rejects_unversioned_database_with_additional_telemetry_tables() -> None:
    inspector = _legacy_inspector()
    inspector.get_table_names.return_value = [
        "telemetry_runs",
        "telemetry_events",
        "telemetry_producers",
    ]

    assert classify_schema(inspector) is SchemaState.UNEXPECTED


def test_rejects_legacy_database_with_wrong_columns() -> None:
    inspector = _legacy_inspector()
    columns = inspector.get_columns("telemetry_runs")
    inspector.get_columns.side_effect = lambda table: (
        columns[:-1]
        if table == "telemetry_runs"
        else _legacy_inspector().get_columns(table)
    )

    assert classify_schema(inspector) is SchemaState.UNEXPECTED
