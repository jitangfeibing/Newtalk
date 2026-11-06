"""Preserve original input time for asynchronous memory writes.

Revision ID: 20261027_10
Revises: 20261025_09
"""

import sqlalchemy as sa
from alembic import op


revision = "20261027_10"
down_revision = "20261025_09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 旧任务不知道真实发言时刻，不能拿入队/重试时间回填成用户发言时间。
    op.add_column("memory_jobs", sa.Column("chat_time", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("memory_jobs", "chat_time")
