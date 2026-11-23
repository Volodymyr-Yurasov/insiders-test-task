"""product

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-23 12:13:25.134382

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "product",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("base_price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("min_stock_level", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
        sa.CheckConstraint("base_price >= 0", name=op.f("product_base_price_non_negative_check")),
        sa.CheckConstraint(
            "min_stock_level >= 0", name=op.f("product_min_stock_level_non_negative_check")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("product_pkey")),
        sa.UniqueConstraint("sku", name=op.f("product_sku_key")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("product")
