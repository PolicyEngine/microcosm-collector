"""Record artifact submissions and their worker jobs.

Revision ID: 0002_artifact_intake
Revises: 0001_initial_schema
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_artifact_intake"
down_revision: str | Sequence[str] | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SUBMISSION_STATUSES = (
    "status IN ('awaiting_upload', 'queued', 'processing', 'succeeded', "
    "'rejected', 'failed')"
)
JOB_STATUSES = "status IN ('queued', 'running', 'succeeded', 'rejected', 'failed')"


def upgrade() -> None:
    """Create the submission and job tables.

    This grants nothing. The runtime database user gets its access from the
    migration user's default privileges, set once by hand in each
    environment (README, "Database access").
    """

    op.create_table(
        "artifact_submissions",
        sa.Column("submission_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("owner_hf_id", sa.Text(), nullable=False),
        sa.Column("owner_hf_username", sa.Text(), nullable=False),
        sa.Column("licensed_data", sa.Boolean(), nullable=False),
        sa.Column("provenance", postgresql.JSONB(), nullable=False),
        sa.Column("files", postgresql.JSONB(), nullable=False),
        sa.Column("files_sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("rejection", postgresql.JSONB()),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("processing_round", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            SUBMISSION_STATUSES, name=op.f("ck_artifact_submissions_status")
        ),
        sa.PrimaryKeyConstraint("submission_id", name="pk_artifact_submissions"),
    )
    op.create_table(
        "artifact_jobs",
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("submission_id", sa.Text(), nullable=False),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column("processing_round", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("dispatch_count", sa.Integer(), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("lease_holder", sa.Text()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", postgresql.JSONB()),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(JOB_STATUSES, name=op.f("ck_artifact_jobs_status")),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["artifact_submissions.submission_id"],
            name="fk_artifact_jobs_submission_id_artifact_submissions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("job_id", name="pk_artifact_jobs"),
        sa.UniqueConstraint(
            "submission_id",
            "processing_round",
            name="artifact_jobs_submission_round_key",
        ),
    )
    op.create_index("artifact_jobs_status_idx", "artifact_jobs", ["status"])


def downgrade() -> None:
    """Remove the submission and job tables, keeping the telemetry schema."""

    op.drop_table("artifact_jobs")
    op.drop_table("artifact_submissions")
