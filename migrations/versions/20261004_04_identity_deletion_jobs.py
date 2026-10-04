"""Add durable identity deletion jobs.

Revision ID: 20261004_04
Revises: 20261004_03
Create Date: 2026-10-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20261004_04"
down_revision: str | None = "20261004_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "identity_deletion_jobs",
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("device_id", sa.String(length=17), nullable=False),
        sa.Column("identity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_identity_deletion_jobs_status",
        ),
        sa.PrimaryKeyConstraint("job_id"),
        sa.UniqueConstraint("identity_id", name="uq_identity_deletion_jobs_identity_id"),
    )
    op.create_index(
        "ix_identity_deletion_jobs_device_id",
        "identity_deletion_jobs",
        ["device_id"],
        unique=False,
    )
    op.create_index(
        "ix_identity_deletion_jobs_identity_id",
        "identity_deletion_jobs",
        ["identity_id"],
        unique=True,
    )
    op.create_index(
        "ix_identity_deletion_jobs_pending",
        "identity_deletion_jobs",
        ["status", "available_at", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_identity_deletion_jobs_pending", table_name="identity_deletion_jobs")
    op.drop_index("ix_identity_deletion_jobs_identity_id", table_name="identity_deletion_jobs")
    op.drop_index("ix_identity_deletion_jobs_device_id", table_name="identity_deletion_jobs")
    op.drop_table("identity_deletion_jobs")
