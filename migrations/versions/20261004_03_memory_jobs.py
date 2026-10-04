"""Add persistent Memory write jobs.

Revision ID: 20261004_03
Revises: 20260829_02
Create Date: 2026-10-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20261004_03"
down_revision: str | None = "20260829_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_jobs",
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", sa.String(length=36), nullable=False),
        sa.Column("device_id", sa.String(length=17), nullable=False),
        sa.Column("identity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("speaker_name", sa.String(length=80), nullable=False),
        sa.Column("user_text", sa.Text(), nullable=False),
        sa.Column("assistant_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("provider_task_id", sa.String(length=160), nullable=True),
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
            name="ck_memory_jobs_status",
        ),
        sa.ForeignKeyConstraint(["device_id"], ["devices.device_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["identity_id"], ["identities.identity_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("job_id"),
        sa.UniqueConstraint("turn_id", name="uq_memory_jobs_turn_id"),
    )
    op.create_index(
        "ix_memory_jobs_device_id", "memory_jobs", ["device_id"], unique=False
    )
    op.create_index(
        "ix_memory_jobs_identity_id", "memory_jobs", ["identity_id"], unique=False
    )
    op.create_index(
        "ix_memory_jobs_pending",
        "memory_jobs",
        ["status", "available_at", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_memory_jobs_pending", table_name="memory_jobs")
    op.drop_index("ix_memory_jobs_identity_id", table_name="memory_jobs")
    op.drop_index("ix_memory_jobs_device_id", table_name="memory_jobs")
    op.drop_table("memory_jobs")
