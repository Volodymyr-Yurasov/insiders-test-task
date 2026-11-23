"""refresh_token

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-23 11:48:03.061081

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "refresh_token",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
            ["user_id"],
            ["app_user.id"],
            name=op.f("refresh_token_user_id_fkey"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("refresh_token_pkey")),
        sa.UniqueConstraint("token_hash", name=op.f("refresh_token_token_hash_key")),
    )
    op.create_index(
        op.f("refresh_token_family_id_idx"), "refresh_token", ["family_id"], unique=False
    )
    op.create_index(op.f("refresh_token_user_id_idx"), "refresh_token", ["user_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("refresh_token_user_id_idx"), table_name="refresh_token")
    op.drop_index(op.f("refresh_token_family_id_idx"), table_name="refresh_token")
    op.drop_table("refresh_token")
