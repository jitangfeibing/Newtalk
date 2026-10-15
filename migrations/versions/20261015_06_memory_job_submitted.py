"""Track submitted MemOS tasks until remote completion.

Revision ID: 20261015_06
Revises: 20261004_05
Create Date: 2026-10-15
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261015_06"
down_revision: str | None = "20261004_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_memory_jobs_status", "memory_jobs", type_="check")
    op.create_check_constraint(
        "ck_memory_jobs_status",
        "memory_jobs",
        "status IN ('pending', 'processing', 'submitted', 'completed', 'failed')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE memory_jobs SET status = 'pending', provider_task_id = NULL "
        "WHERE status = 'submitted'"
    )
    op.drop_constraint("ck_memory_jobs_status", "memory_jobs", type_="check")
    op.create_check_constraint(
        "ck_memory_jobs_status",
        "memory_jobs",
        "status IN ('pending', 'processing', 'completed', 'failed')",
    )
