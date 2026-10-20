"""Persist device recovery rate limits across app workers.

Revision ID: 20261020_07
Revises: 20261015_06
Create Date: 2026-10-20
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261020_07"
down_revision: str | None = "20261015_06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recovery_rate_limits",
        sa.Column("client_key_digest", sa.String(length=64), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("client_key_digest"),
    )


def downgrade() -> None:
    op.drop_table("recovery_rate_limits")
