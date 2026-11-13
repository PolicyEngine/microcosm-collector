"""Record the calibration tree builds the service queues.

Revision ID: 0004_tree_build_requests
Revises: 0003_orrery_graph_files
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_tree_build_requests"
down_revision: str | Sequence[str] | None = "0003_orrery_graph_files"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = "kind IN ('release', 'staging')"


def upgrade() -> None:
    """Create the table of queued calibration tree builds, one row per task.

    This grants nothing. The runtime database user gets its access from the
    migration user's default privileges, set once by hand in each
    environment (README, "Database access").
    """

    op.create_table(
        "calibration_tree_build_requests",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("country", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("first_requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("last_caller_project_id", sa.Text(), nullable=False),
        sa.Column("last_caller_environment", sa.Text(), nullable=False),
        sa.Column("hf_repo", sa.Text(), nullable=False),
        sa.Column("last_hf_ref", sa.Text()),
        sa.Column("last_hf_commit", sa.Text()),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True)),
        sa.Column("execution", sa.Text()),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("summary", postgresql.JSONB()),
        sa.CheckConstraint(KINDS, name=op.f("ck_calibration_tree_build_requests_kind")),
        sa.PrimaryKeyConstraint("task_id", name="pk_calibration_tree_build_requests"),
    )


def downgrade() -> None:
    """Remove the table, keeping every earlier one.

    Queued tasks stay in Cloud Tasks and still start the worker job, and those
    executions fail: this release's code finds no table to record the attempt
    on, and an older image has no ``reconcile-calibration-trees`` command.
    """

    op.drop_table("calibration_tree_build_requests")
