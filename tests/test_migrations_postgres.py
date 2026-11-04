from __future__ import annotations

import re
from dataclasses import dataclass

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB

from telemetry_collector.database import create_database_engine
from telemetry_collector.migrate import (
    SchemaState,
    alembic_config,
    upgrade_database,
    verify_database,
)

HEAD_REVISION = "0002_artifact_intake"
TELEMETRY_TABLES = {"telemetry_runs", "telemetry_producers", "telemetry_events"}
ARTIFACT_TABLES = {"artifact_submissions", "artifact_jobs"}


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
            *TELEMETRY_TABLES,
            *ARTIFACT_TABLES,
        }
        assert _current_revision(postgres_url) == (HEAD_REVISION, HEAD_REVISION)
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
            *TELEMETRY_TABLES,
            *ARTIFACT_TABLES,
        }
        metadata = sa.MetaData()
        runs = sa.Table("telemetry_runs", metadata, autoload_with=engine)
        events = sa.Table("telemetry_events", metadata, autoload_with=engine)
        with engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(events)) == 0
            )
        assert _current_revision(postgres_url) == (HEAD_REVISION, HEAD_REVISION)
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
        assert _current_revision(postgres_url) == (HEAD_REVISION, HEAD_REVISION)
    finally:
        engine.dispose()


def test_verification_rejects_schema_drift_at_alembic_head(
    postgres_url: str,
) -> None:
    upgrade_database(postgres_url)
    engine = create_database_engine(postgres_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text("ALTER TABLE telemetry_runs ADD COLUMN unexpected TEXT")
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="differs from SQLAlchemy metadata"):
        verify_database(postgres_url)


def test_downgrading_artifact_intake_keeps_the_telemetry_schema(
    postgres_url: str,
) -> None:
    upgrade_database(postgres_url)
    engine = create_database_engine(postgres_url)
    config = alembic_config()
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0001_initial_schema")
        assert set(inspect(engine).get_table_names()) == {
            "alembic_version",
            *TELEMETRY_TABLES,
        }
        assert _current_revision(postgres_url) == (
            "0001_initial_schema",
            HEAD_REVISION,
        )
    finally:
        engine.dispose()


MIGRATION_ROLE = "collector_migration_privilege_test"
RUNTIME_ROLE = "collector_runtime_privilege_test"
TELEMETRY_ONLY_ROLE = "collector_telemetry_only_privilege_test"
UNGRANTED_ROLE = "collector_ungranted_privilege_test"
TEST_ROLES = (RUNTIME_ROLE, TELEMETRY_ONLY_ROLE, UNGRANTED_ROLE, MIGRATION_ROLE)
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")
# The one-time manual step in the README's "Database access": every table the
# migration user creates in public is granted to the runtime user.
RUNTIME_DEFAULT_PRIVILEGES = (
    f"ALTER DEFAULT PRIVILEGES FOR ROLE {MIGRATION_ROLE} IN SCHEMA public "
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {RUNTIME_ROLE}"
)
GRANT_STATEMENT = re.compile(r"^\s*GRANT\b", re.IGNORECASE)


@dataclass(frozen=True)
class IntakeUpgrade:
    """What upgrading from the telemetry schema to the intake schema did."""

    statements: list[str]
    privileges_by_role: dict[str, dict[str, set[str]]]


def _drop_test_roles(connection: sa.Connection) -> None:
    for role in TEST_ROLES:
        exists = connection.scalar(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
        )
        if exists:
            connection.execute(sa.text(f"DROP OWNED BY {role}"))
            connection.execute(sa.text(f"DROP ROLE {role}"))


def _table_privileges(connection: sa.Connection, role: str) -> dict[str, set[str]]:
    return {
        table: {
            privilege
            for privilege in TABLE_PRIVILEGES
            if connection.scalar(
                sa.text("SELECT has_table_privilege(:role, :table, :privilege)"),
                {"role": role, "table": table, "privilege": privilege},
            )
        }
        for table in sorted(ARTIFACT_TABLES)
    }


def _upgrade_to_intake_with_default_privileges(postgres_url: str) -> IntakeUpgrade:
    """Upgrade as a deployed database is upgraded, recording what 0002 runs.

    The migration role owns the tables, as the migration user does. The
    telemetry tables are granted by hand, as the deployments' were, to the
    runtime role and to a role the default privileges don't name; then the
    migration role's default privileges are set as the manual step sets them.
    """

    engine = create_database_engine(postgres_url)
    config = alembic_config()
    statements: list[str] = []

    def record_statement(
        executing_connection: sa.Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    try:
        with engine.begin() as connection:
            _drop_test_roles(connection)
            for role in TEST_ROLES:
                connection.execute(sa.text(f"CREATE ROLE {role} NOLOGIN"))
            connection.execute(
                sa.text(f"GRANT CREATE ON SCHEMA public TO {MIGRATION_ROLE}")
            )
            connection.execute(sa.text(f"SET LOCAL ROLE {MIGRATION_ROLE}"))
            config.attributes["connection"] = connection
            command.upgrade(config, "0001_initial_schema")
            connection.execute(
                sa.text(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON telemetry_runs, "
                    f"telemetry_producers, telemetry_events TO {RUNTIME_ROLE}"
                )
            )
            connection.execute(
                sa.text(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON telemetry_runs "
                    f"TO {TELEMETRY_ONLY_ROLE}"
                )
            )
            connection.execute(sa.text(RUNTIME_DEFAULT_PRIVILEGES))
            sa.event.listen(connection, "before_cursor_execute", record_statement)
            try:
                command.upgrade(config, "head")
            finally:
                sa.event.remove(connection, "before_cursor_execute", record_statement)
            privileges_by_role = {
                role: _table_privileges(connection, role)
                for role in (RUNTIME_ROLE, TELEMETRY_ONLY_ROLE, UNGRANTED_ROLE)
            }
        return IntakeUpgrade(statements, privileges_by_role)
    finally:
        with engine.begin() as connection:
            _drop_test_roles(connection)
        engine.dispose()


def test_artifact_intake_migration_issues_no_grant(postgres_url: str) -> None:
    upgrade = _upgrade_to_intake_with_default_privileges(postgres_url)

    assert any("CREATE TABLE artifact_jobs" in sql for sql in upgrade.statements)
    assert [sql for sql in upgrade.statements if GRANT_STATEMENT.match(sql)] == []


def test_default_privileges_give_the_runtime_role_the_intake_tables(
    postgres_url: str,
) -> None:
    upgrade = _upgrade_to_intake_with_default_privileges(postgres_url)

    assert upgrade.privileges_by_role[RUNTIME_ROLE] == {
        table: set(TABLE_PRIVILEGES) for table in ARTIFACT_TABLES
    }
    assert upgrade.privileges_by_role[UNGRANTED_ROLE] == {
        table: set() for table in ARTIFACT_TABLES
    }


def test_artifact_intake_gives_telemetry_grantees_no_intake_access(
    postgres_url: str,
) -> None:
    upgrade = _upgrade_to_intake_with_default_privileges(postgres_url)

    assert upgrade.privileges_by_role[TELEMETRY_ONLY_ROLE] == {
        table: set() for table in ARTIFACT_TABLES
    }
