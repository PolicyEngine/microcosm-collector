from __future__ import annotations

import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from telemetry_collector.database import create_database_engine
from telemetry_collector.migrate import SchemaState, alembic_config, upgrade_database


def _current_revision(database_url: str) -> tuple[str | None, str | None]:
    engine = create_database_engine(database_url)
    config = alembic_config()
    try:
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
        head = ScriptDirectory.from_config(config).get_current_head()
        return current, head
    finally:
        engine.dispose()


def test_upgrades_empty_postgres_database_to_head(postgres_url: str) -> None:
    assert upgrade_database(postgres_url) is SchemaState.EMPTY

    engine = create_database_engine(postgres_url)
    try:
        assert set(inspect(engine).get_table_names()) == {
            "alembic_version",
            "telemetry_runs",
            "telemetry_producers",
            "telemetry_events",
        }
        assert _current_revision(postgres_url) == (
            "0002_orm_schema",
            "0002_orm_schema",
        )
    finally:
        engine.dispose()


def test_adopts_populated_legacy_database(postgres_url: str) -> None:
    engine = create_database_engine(postgres_url)
    config = alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0001_legacy_schema")
        metadata = sa.MetaData()
        runs = sa.Table("telemetry_runs", metadata, autoload_with=connection)
        events = sa.Table("telemetry_events", metadata, autoload_with=connection)
        timestamp = sa.func.current_timestamp()
        connection.execute(
            runs.insert().values(
                run_id="legacy-run",
                country_code="US",
                pipeline="us-fiscal-refresh",
                run_kind="build",
                owner_hf_id="owner",
                owner_hf_username="builder",
                status="running",
                current_stage="created",
                started_at=timestamp,
                updated_at=timestamp,
            )
        )
        connection.execute(
            events.insert().values(
                event_id="legacy-event",
                run_id="legacy-run",
                producer_id="legacy-producer",
                sequence=1,
                emitted_at=timestamp,
                event_type="stage",
                stage_id="compile",
                status="started",
                details={},
            )
        )
        sa.Table("alembic_version", metadata, autoload_with=connection).drop(connection)
    engine.dispose()

    assert upgrade_database(postgres_url) is SchemaState.LEGACY

    engine = create_database_engine(postgres_url)
    try:
        metadata = sa.MetaData()
        producers = sa.Table("telemetry_producers", metadata, autoload_with=engine)
        with engine.connect() as connection:
            rows = connection.execute(sa.select(producers)).mappings().all()
        assert [(row["run_id"], row["producer_id"]) for row in rows] == [
            ("legacy-run", "legacy-producer")
        ]
        assert rows[0]["registered_at"] is not None
        assert _current_revision(postgres_url) == (
            "0002_orm_schema",
            "0002_orm_schema",
        )
    finally:
        engine.dispose()


def test_migration_chain_downgrades_and_reupgrades_in_test_database(
    postgres_url: str,
) -> None:
    upgrade_database(postgres_url)
    engine = create_database_engine(postgres_url)
    config = alembic_config()
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "base")
            command.upgrade(config, "head")
        assert _current_revision(postgres_url) == (
            "0002_orm_schema",
            "0002_orm_schema",
        )
    finally:
        engine.dispose()
