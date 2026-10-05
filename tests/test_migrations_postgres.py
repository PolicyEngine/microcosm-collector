from __future__ import annotations

import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB

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


def _legacy_metadata() -> sa.MetaData:
    metadata = sa.MetaData()
    runs = sa.Table(
        "telemetry_runs",
        metadata,
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("country_code", sa.Text(), nullable=False),
        sa.Column("pipeline", sa.Text(), nullable=False),
        sa.Column("candidate_id", sa.Text()),
        sa.Column("release_id", sa.Text()),
        sa.Column("run_kind", sa.Text(), nullable=False),
        sa.Column("owner_hf_id", sa.Text(), nullable=False),
        sa.Column("owner_hf_username", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("current_stage", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("resources", JSONB()),
        sa.Column("work", JSONB()),
        sa.Column("failure", JSONB()),
    )
    sa.Index(
        "telemetry_runs_country_updated_idx",
        runs.c.country_code,
        runs.c.updated_at.desc(),
    )
    events = sa.Table(
        "telemetry_events",
        metadata,
        sa.Column("event_id", sa.Text(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("telemetry_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("producer_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("emitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("stage_id", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("message", sa.Text()),
        sa.Column("details", JSONB(), server_default="{}", nullable=False),
        sa.Column("resources", JSONB()),
        sa.UniqueConstraint(
            "run_id",
            "producer_id",
            "sequence",
            name="telemetry_events_run_id_producer_id_sequence_key",
        ),
    )
    sa.Index(
        "telemetry_events_run_order_idx",
        events.c.run_id,
        events.c.emitted_at,
        events.c.producer_id,
        events.c.sequence,
    )
    return metadata


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
            "0001_initial_schema",
            "0001_initial_schema",
        )
    finally:
        engine.dispose()


def test_hard_cutover_replaces_populated_unversioned_database(
    postgres_url: str,
) -> None:
    engine = create_database_engine(postgres_url)
    with engine.begin() as connection:
        metadata = _legacy_metadata()
        metadata.create_all(connection)
        runs = metadata.tables["telemetry_runs"]
        events = metadata.tables["telemetry_events"]
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
    engine.dispose()

    assert upgrade_database(postgres_url) is SchemaState.LEGACY

    engine = create_database_engine(postgres_url)
    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) == {
            "alembic_version",
            "telemetry_runs",
            "telemetry_producers",
            "telemetry_events",
        }
        metadata = sa.MetaData()
        runs = sa.Table("telemetry_runs", metadata, autoload_with=engine)
        events = sa.Table("telemetry_events", metadata, autoload_with=engine)
        with engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(events)) == 0
            )
        assert _current_revision(postgres_url) == (
            "0001_initial_schema",
            "0001_initial_schema",
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
            "0001_initial_schema",
            "0001_initial_schema",
        )
    finally:
        engine.dispose()
