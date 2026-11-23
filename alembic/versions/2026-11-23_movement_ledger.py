"""movement_ledger

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-23 12:27:44.768693

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "operation",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "IN",
                "OUT",
                "TRANSFER",
                name="movement_type",
                native_enum=False,
                create_constraint=True,
                length=10,
            ),
            nullable=False,
        ),
        sa.Column("performed_by", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["performed_by"],
            ["app_user.id"],
            name=op.f("operation_performed_by_fkey"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("operation_pkey")),
        sa.UniqueConstraint(
            "performed_by",
            "idempotency_key",
            name=op.f("operation_performed_by_idempotency_key_key"),
        ),
    )
    op.create_table(
        "stock_movement",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("operation_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "IN",
                "OUT",
                "TRANSFER",
                name="movement_type",
                native_enum=False,
                create_constraint=True,
                length=10,
            ),
            nullable=False,
        ),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("from_warehouse_id", sa.Integer(), nullable=True),
        sa.Column("to_warehouse_id", sa.Integer(), nullable=True),
        sa.Column("performed_by", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(type = 'IN' AND from_warehouse_id IS NULL AND to_warehouse_id IS NOT NULL)"
            " OR (type = 'OUT' AND from_warehouse_id IS NOT NULL AND to_warehouse_id IS NULL)"
            " OR (type = 'TRANSFER' AND from_warehouse_id IS NOT NULL"
            " AND to_warehouse_id IS NOT NULL AND from_warehouse_id <> to_warehouse_id)",
            name=op.f("stock_movement_movement_shape_check"),
        ),
        sa.CheckConstraint(
            "quantity BETWEEN 1 AND 1000000", name=op.f("stock_movement_quantity_range_check")
        ),
        sa.ForeignKeyConstraint(
            ["from_warehouse_id"],
            ["warehouse.id"],
            name=op.f("stock_movement_from_warehouse_id_fkey"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operation.id"],
            name=op.f("stock_movement_operation_id_fkey"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["performed_by"],
            ["app_user.id"],
            name=op.f("stock_movement_performed_by_fkey"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["product.id"],
            name=op.f("stock_movement_product_id_fkey"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["to_warehouse_id"],
            ["warehouse.id"],
            name=op.f("stock_movement_to_warehouse_id_fkey"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("stock_movement_pkey")),
    )
    op.create_index(
        op.f("stock_movement_created_at_idx"), "stock_movement", ["created_at"], unique=False
    )
    op.create_index(
        op.f("stock_movement_from_warehouse_id_idx"),
        "stock_movement",
        ["from_warehouse_id"],
        unique=False,
    )
    op.create_index(
        op.f("stock_movement_operation_id_idx"), "stock_movement", ["operation_id"], unique=False
    )
    op.create_index(
        op.f("stock_movement_product_id_from_warehouse_id_idx"),
        "stock_movement",
        ["product_id", "from_warehouse_id"],
        unique=False,
    )
    op.create_index(
        op.f("stock_movement_product_id_to_warehouse_id_idx"),
        "stock_movement",
        ["product_id", "to_warehouse_id"],
        unique=False,
    )
    op.create_index(
        op.f("stock_movement_to_warehouse_id_idx"),
        "stock_movement",
        ["to_warehouse_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("stock_movement_to_warehouse_id_idx"), table_name="stock_movement")
    op.drop_index(
        op.f("stock_movement_product_id_to_warehouse_id_idx"), table_name="stock_movement"
    )
    op.drop_index(
        op.f("stock_movement_product_id_from_warehouse_id_idx"), table_name="stock_movement"
    )
    op.drop_index(op.f("stock_movement_operation_id_idx"), table_name="stock_movement")
    op.drop_index(op.f("stock_movement_from_warehouse_id_idx"), table_name="stock_movement")
    op.drop_index(op.f("stock_movement_created_at_idx"), table_name="stock_movement")
    op.drop_table("stock_movement")
    op.drop_table("operation")
