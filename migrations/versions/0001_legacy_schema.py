"""Represent the collector schema that predates Alembic.

Revision ID: 0001_legacy_schema
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_legacy_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the exact schema originally installed by startup SQL."""

    op.create_table(
        "telemetry_runs",
        sa.Column("run_id", sa.Text(), nullable=False),
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
        sa.Column("resources", postgresql.JSONB()),
        sa.Column("work", postgresql.JSONB()),
        sa.Column("failure", postgresql.JSONB()),
        sa.PrimaryKeyConstraint("run_id", name="telemetry_runs_pkey"),
    )
    op.create_index(
        "telemetry_runs_country_updated_idx",
        "telemetry_runs",
        ["country_code", sa.column("updated_at").desc()],
    )
    op.create_table(
        "telemetry_events",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
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
        sa.Column("details", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("resources", postgresql.JSONB()),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["telemetry_runs.run_id"],
            name="telemetry_events_run_id_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", name="telemetry_events_pkey"),
        sa.UniqueConstraint(
            "run_id",
            "producer_id",
            "sequence",
            name="telemetry_events_run_id_producer_id_sequence_key",
        ),
    )
    op.create_index(
        "telemetry_events_run_order_idx",
        "telemetry_events",
        ["run_id", "emitted_at", "producer_id", "sequence"],
    )


def downgrade() -> None:
    """Remove the legacy collector schema."""

    op.drop_table("telemetry_events")
    op.drop_table("telemetry_runs")
