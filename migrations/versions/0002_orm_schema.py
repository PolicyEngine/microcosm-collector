"""Add stable registration and producer identity.

Revision ID: 0002_orm_schema
Revises: 0001_legacy_schema
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_orm_schema"
down_revision: str | Sequence[str] | None = "0001_legacy_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade legacy telemetry tables to the SQLAlchemy ORM schema."""

    bind = op.get_bind()
    op.add_column(
        "telemetry_runs",
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=True),
    )
    runs = sa.table(
        "telemetry_runs",
        sa.column("run_id", sa.Text()),
        sa.column("started_at", sa.DateTime(timezone=True)),
        sa.column("registered_at", sa.DateTime(timezone=True)),
    )
    bind.execute(sa.update(runs).values(registered_at=runs.c.started_at))
    op.alter_column(
        "telemetry_runs",
        "registered_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.current_timestamp(),
    )

    op.create_table(
        "telemetry_producers",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("producer_id", sa.Text(), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["telemetry_runs.run_id"],
            name="fk_telemetry_producers_run_id_telemetry_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "producer_id", name="pk_telemetry_producers"),
    )
    events = sa.table(
        "telemetry_events",
        sa.column("run_id", sa.Text()),
        sa.column("producer_id", sa.Text()),
        sa.column("received_at", sa.DateTime(timezone=True)),
    )
    producers = sa.table(
        "telemetry_producers",
        sa.column("run_id", sa.Text()),
        sa.column("producer_id", sa.Text()),
        sa.column("registered_at", sa.DateTime(timezone=True)),
    )
    producer_rows = (
        sa.select(
            events.c.run_id,
            events.c.producer_id,
            sa.func.min(events.c.received_at).label("registered_at"),
        )
        .group_by(events.c.run_id, events.c.producer_id)
        .order_by(events.c.run_id, events.c.producer_id)
    )
    bind.execute(
        sa.insert(producers).from_select(
            ["run_id", "producer_id", "registered_at"], producer_rows
        )
    )
    op.create_index(
        "telemetry_producers_run_registered_idx",
        "telemetry_producers",
        ["run_id", "registered_at", "producer_id"],
    )
    op.create_foreign_key(
        "fk_telemetry_events_run_id_producer_id_telemetry_producers",
        "telemetry_events",
        "telemetry_producers",
        ["run_id", "producer_id"],
        ["run_id", "producer_id"],
        ondelete="CASCADE",
    )

    op.drop_index("telemetry_runs_country_updated_idx", table_name="telemetry_runs")
    op.create_index(
        "telemetry_runs_country_registered_idx",
        "telemetry_runs",
        ["country_code", sa.column("registered_at").desc(), sa.column("run_id").desc()],
    )
    op.drop_index("telemetry_events_run_order_idx", table_name="telemetry_events")
    op.create_index(
        "telemetry_events_run_order_idx",
        "telemetry_events",
        ["run_id", "producer_id", "sequence"],
    )


def downgrade() -> None:
    """Restore the pre-Alembic collector schema."""

    op.drop_index("telemetry_events_run_order_idx", table_name="telemetry_events")
    op.create_index(
        "telemetry_events_run_order_idx",
        "telemetry_events",
        ["run_id", "emitted_at", "producer_id", "sequence"],
    )
    op.drop_index("telemetry_runs_country_registered_idx", table_name="telemetry_runs")
    op.create_index(
        "telemetry_runs_country_updated_idx",
        "telemetry_runs",
        ["country_code", sa.column("updated_at").desc()],
    )
    op.drop_constraint(
        "fk_telemetry_events_run_id_producer_id_telemetry_producers",
        "telemetry_events",
        type_="foreignkey",
    )
    op.drop_table("telemetry_producers")
    op.drop_column("telemetry_runs", "registered_at")
