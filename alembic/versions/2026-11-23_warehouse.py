"""warehouse

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-23 12:12:06.759062

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "warehouse",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("location_code", sa.String(length=32), nullable=False),
        sa.Column("address", sa.String(length=500), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("warehouse_pkey")),
        sa.UniqueConstraint("location_code", name=op.f("warehouse_location_code_key")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("warehouse")
