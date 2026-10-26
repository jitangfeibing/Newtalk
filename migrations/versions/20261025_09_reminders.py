"""Add device-scoped one-off reminders and playback receipts.

Revision ID: 20261025_09
Revises: 20261022_08
"""
import sqlalchemy as sa
from alembic import op

revision = "20261025_09"
down_revision = "20261022_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reminders",
        sa.Column("reminder_id", sa.String(36), primary_key=True),
        sa.Column("device_id", sa.String(17), sa.ForeignKey("devices.device_id", ondelete="CASCADE"), nullable=False),
        sa.Column("content", sa.String(160), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("delivery_id", sa.String(36)),
        sa.Column("delivery_status", sa.String(16)),
        sa.Column("connection_id", sa.String(36)),
        sa.Column("delivery_deadline", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("device_id", "request_key", name="uq_reminders_device_request"),
    )
    op.create_index("ix_reminders_status_due", "reminders", ["status", "due_at"])
    op.create_index("ix_reminders_device_due", "reminders", ["device_id", "due_at"])


def downgrade() -> None:
    op.drop_index("ix_reminders_device_due", table_name="reminders")
    op.drop_index("ix_reminders_status_due", table_name="reminders")
    op.drop_table("reminders")
