"""Persist polling budgets for asynchronous memory writes.

Revision ID: 20261022_08
Revises: 20261020_07
Create Date: 2026-10-22
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261022_08"
down_revision: str | None = "20261020_07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memory_jobs",
        sa.Column("poll_count", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("memory_jobs", "poll_count")
