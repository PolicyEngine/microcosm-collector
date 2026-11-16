"""Record the Orrery graph files the worker publishes from submissions.

Revision ID: 0003_orrery_graph_files
Revises: 0002_artifact_intake
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_orrery_graph_files"
down_revision: str | Sequence[str] | None = "0002_artifact_intake"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VISIBILITIES = "visibility IN ('public', 'private')"


def upgrade() -> None:
    """Create the table of published Orrery graph files.

    This grants nothing. The runtime database user gets its access from the
    migration user's default privileges, set once by hand in each
    environment (README, "Database access").
    """

    op.create_table(
        "orrery_graph_files",
        sa.Column("submission_id", sa.Text(), nullable=False),
        sa.Column("processing_round", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("build_id", sa.Text(), nullable=False),
        sa.Column("country_code", sa.Text(), nullable=False),
        sa.Column("object_prefix", sa.Text(), nullable=False),
        sa.Column("document_sha256", sa.Text(), nullable=False),
        sa.Column("document_bytes", sa.BigInteger(), nullable=False),
        sa.Column("upstream_files", sa.Integer(), nullable=False),
        sa.Column("orrery_version", sa.Text(), nullable=False),
        sa.Column("projection_source", sa.Text(), nullable=False),
        sa.Column("collector_version", sa.Text(), nullable=False),
        sa.Column("reconstructed", sa.Boolean(), nullable=False),
        sa.Column("visibility", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(VISIBILITIES, name=op.f("ck_orrery_graph_files_visibility")),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["artifact_submissions.submission_id"],
            name="fk_orrery_graph_files_submission_id_artifact_submissions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "submission_id", "document_sha256", name="pk_orrery_graph_files"
        ),
    )


def downgrade() -> None:
    """Remove the table, keeping the intake and telemetry tables.

    The graph files stay in the Orrery bucket; only their records go.
    """

    op.drop_table("orrery_graph_files")
