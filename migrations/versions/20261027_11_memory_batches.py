"""Persist memory batches, frozen sources and prepared payloads.

Revision ID: 20261027_11
Revises: 20261027_10
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261027_11"
down_revision = "20261027_10"
branch_labels = None
depends_on = None


def _scope():
    return [
        sa.Column(
            "device_id",
            sa.String(17),
            sa.ForeignKey("devices.device_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "identity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("identities.identity_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("segment_id", sa.String(160), nullable=False),
        sa.Column("speaker_name", sa.String(80), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "memory_batches",
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_scope(),
        sa.Column("sources", postgresql.JSONB(), nullable=False),
        sa.Column("prepared_payload", postgresql.JSONB(), nullable=True),
        sa.Column("first_source_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(160), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(36), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'queued', 'skipped', 'failed')",
            name="ck_memory_batches_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_memory_batches_attempts"),
    )
    op.create_table(
        "memory_sources",
        sa.Column("source_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("turn_id", sa.String(36), nullable=False, unique=True),
        *_scope(),
        sa.Column("user_text", sa.Text(), nullable=False),
        sa.Column("assistant_text", sa.Text(), nullable=True),
        sa.Column("chat_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled", sa.Boolean(), nullable=False),
        sa.Column(
            "batch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memory_batches.batch_id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for table in ("memory_batches", "memory_sources"):
        for column in ("device_id", "identity_id"):
            op.create_index(f"ix_{table}_{column}", table, [column])
    op.create_index("ix_memory_sources_batch_id", "memory_sources", ["batch_id"])
    op.add_column(
        "memory_jobs", sa.Column("prepared_payload", postgresql.JSONB(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("memory_jobs", "prepared_payload")
    op.drop_table("memory_sources")
    op.drop_table("memory_batches")
