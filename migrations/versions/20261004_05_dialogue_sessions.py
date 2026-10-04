"""Add persistent Dialogue sessions and exchanges.

Revision ID: 20261004_05
Revises: 20261004_04
Create Date: 2026-10-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20261004_05"
down_revision: str | None = "20261004_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dialogue_sessions",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("device_id", sa.String(length=17), nullable=False),
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
        sa.ForeignKeyConstraint(["device_id"], ["devices.device_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id"),
        sa.UniqueConstraint("device_id", name="uq_dialogue_sessions_device_id"),
    )
    op.create_index(
        "ix_dialogue_sessions_device_id",
        "dialogue_sessions",
        ["device_id"],
        unique=True,
    )
    op.create_table(
        "dialogue_exchanges",
        sa.Column("exchange_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", sa.String(length=36), nullable=False),
        sa.Column("lane", sa.String(length=12), nullable=False),
        sa.Column("user_text", sa.Text(), nullable=False),
        sa.Column("assistant_text", sa.Text(), nullable=False),
        sa.Column("speaker_identity_id", sa.String(length=36), nullable=True),
        sa.Column("speaker_display_name", sa.String(length=80), nullable=False),
        sa.Column("speaker_relationship", sa.String(length=80), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "lane IN ('family', 'guest')",
            name="ck_dialogue_exchanges_lane",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["dialogue_sessions.session_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("exchange_id"),
        sa.UniqueConstraint("turn_id", name="uq_dialogue_exchanges_turn_id"),
    )
    op.create_index(
        "ix_dialogue_exchanges_session_id",
        "dialogue_exchanges",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        "ix_dialogue_exchanges_window",
        "dialogue_exchanges",
        ["session_id", "lane", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_dialogue_exchanges_window", table_name="dialogue_exchanges")
    op.drop_index("ix_dialogue_exchanges_session_id", table_name="dialogue_exchanges")
    op.drop_table("dialogue_exchanges")
    op.drop_index("ix_dialogue_sessions_device_id", table_name="dialogue_sessions")
    op.drop_table("dialogue_sessions")
