"""Add one voiceprint template per identity.

Revision ID: 20260829_02
Revises: 20260828_01
Create Date: 2026-08-29
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260829_02"
down_revision: str | None = "20260828_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("identities", sa.Column("voiceprint_embedding", sa.LargeBinary(), nullable=True))
    op.add_column(
        "identities",
        sa.Column("voiceprint_embedding_dimension", sa.Integer(), nullable=True),
    )
    op.add_column("identities", sa.Column("voiceprint_model", sa.String(length=160), nullable=True))
    op.add_column(
        "identities",
        sa.Column("voiceprint_enrolled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_identities_voiceprint_complete",
        "identities",
        "(voiceprint_embedding IS NULL AND voiceprint_embedding_dimension IS NULL "
        "AND voiceprint_model IS NULL AND voiceprint_enrolled_at IS NULL) OR "
        "(voiceprint_embedding IS NOT NULL AND voiceprint_embedding_dimension > 0 "
        "AND octet_length(voiceprint_embedding) = voiceprint_embedding_dimension * 4 "
        "AND voiceprint_model IS NOT NULL AND voiceprint_enrolled_at IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_identities_voiceprint_complete",
        "identities",
        type_="check",
    )
    op.drop_column("identities", "voiceprint_enrolled_at")
    op.drop_column("identities", "voiceprint_model")
    op.drop_column("identities", "voiceprint_embedding_dimension")
    op.drop_column("identities", "voiceprint_embedding")
