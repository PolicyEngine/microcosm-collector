from __future__ import annotations

import ast
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.pool import QueuePool

from telemetry_collector.database import (
    DB_MAX_OVERFLOW,
    DB_POOL_SIZE,
    ArtifactJob,
    ArtifactSubmission,
    Base,
    TelemetryEvent,
    TelemetryProducer,
    TelemetryRun,
    create_database_engine,
    normalize_database_url,
)


def test_orm_metadata_defines_telemetry_and_artifact_intake_tables() -> None:
    assert set(Base.metadata.tables) == {
        "telemetry_runs",
        "telemetry_producers",
        "telemetry_events",
        "artifact_submissions",
        "artifact_jobs",
        "orrery_graph_files",
        "calibration_tree_build_requests",
    }
    assert set(inspect(ArtifactSubmission).relationships.keys()) == {"jobs"}
    assert set(inspect(ArtifactJob).relationships.keys()) == {"submission"}
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


def _migration(file_name: str = "0002_artifact_intake.py"):
    import importlib.util

    migration_path = (
        Path(__file__).resolve().parents[1] / "migrations" / "versions" / file_name
    )
    specification = importlib.util.spec_from_file_location(
        migration_path.stem, migration_path
    )
    assert specification is not None and specification.loader is not None
    migration = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(migration)
    return migration


# Alembic records the current revision in `alembic_version.version_num`,
# which it creates as `VARCHAR(32)`. A longer revision ID makes every upgrade
# to that revision fail on Postgres.
ALEMBIC_VERSION_NUM_LENGTH = 32


def test_every_revision_id_fits_alembic_s_version_column() -> None:
    versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    revision_ids = {
        source.name: _migration(source.name).revision
        for source in sorted(versions.glob("*.py"))
    }

    assert revision_ids
    assert {
        file_name: revision_id
        for file_name, revision_id in revision_ids.items()
        if len(revision_id) > ALEMBIC_VERSION_NUM_LENGTH
    } == {}


def test_every_migration_file_is_named_after_its_revision_id() -> None:
    versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    sources = sorted(versions.glob("*.py"))

    assert sources
    assert {
        source.name: _migration(source.name).revision
        for source in sources
        if _migration(source.name).revision != source.stem
    } == {}


def _status_constraint(table) -> str:
    [constraint] = [
        constraint
        for constraint in table.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    ]
    return str(constraint.sqltext)


def test_the_migration_keeps_the_models_status_sets() -> None:
    migration = _migration()

    assert migration.SUBMISSION_STATUSES == _status_constraint(
        ArtifactSubmission.__table__
    )
    assert migration.JOB_STATUSES == _status_constraint(ArtifactJob.__table__)


def test_the_orrery_graph_migration_keeps_the_models_visibilities() -> None:
    import importlib

    from telemetry_collector.disclosure import output_visibility

    database = importlib.import_module("telemetry_collector.database")
    migration = _migration("0003_orrery_graph_files.py")

    assert migration.down_revision == "0002_artifact_intake"
    assert migration.VISIBILITIES == _status_constraint(
        database.OrreryGraphFile.__table__
    )
    # Every visibility a worker can record passes the check.
    for licensed_data in (True, False):
        visibility = output_visibility("orrery_artifact", licensed_data=licensed_data)
        assert f"'{visibility}'" in migration.VISIBILITIES


def test_the_build_request_migration_keeps_the_model_s_kinds() -> None:
    import importlib

    database = importlib.import_module("telemetry_collector.database")
    model = getattr(database, "CalibrationTreeBuildRequest", None)
    assert model is not None, "telemetry_collector.database has no build request model."
    migration = _migration("0004_calibration_tree_build_requests.py")

    assert migration.down_revision == "0003_orrery_graph_files"
    assert migration.KINDS == _status_constraint(model.__table__)
    assert migration.KINDS == "kind IN ('release', 'staging')"
