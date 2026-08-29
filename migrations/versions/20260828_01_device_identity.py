"""Create devices and identities.

Revision ID: 20260828_01
Revises:
Create Date: 2026-08-28
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260828_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "devices",
        sa.Column("device_id", sa.String(length=17), nullable=False),
        sa.Column("credential_digest", sa.String(length=64), nullable=False),
        sa.Column("recovery_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("device_id"),
        sa.UniqueConstraint("credential_digest"),
        sa.UniqueConstraint("recovery_digest"),
    )
    op.create_table(
        "identities",
        sa.Column(
            "identity_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("device_id", sa.String(length=17), nullable=False),
        sa.Column("display_name", sa.String(length=80), nullable=False),
        sa.Column("nickname", sa.String(length=80), nullable=True),
        sa.Column("relationship", sa.String(length=80), nullable=True),
        sa.Column("avatar", sa.String(length=500), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.device_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("identity_id"),
    )
    op.create_index(
        op.f("ix_identities_device_id"),
        "identities",
        ["device_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_identities_device_id"), table_name="identities")
    op.drop_table("identities")
    op.drop_table("devices")
