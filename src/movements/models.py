"""Append-only movement ledger. Stock is never stored: it's always derived from these rows.

Ledger rows are never updated, so they carry created_at only (no TimestampMixin), and every
foreign key is RESTRICT so nothing can delete history out from under the stock numbers.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base
from src.movements.constants import MAX_QUANTITY, MovementType


def _movement_type() -> Enum:
    # A fresh Enum per table: each table gets its own named CHECK constraint.
    return Enum(
        MovementType,
        name="movement_type",
        native_enum=False,
        create_constraint=True,
        length=10,
        values_callable=lambda types: [t.value for t in types],
    )


class Operation(Base):
    """One API request: a single movement or a whole batch."""

    __tablename__ = "operation"
    # Many NULL keys are allowed (Postgres treats NULLs as distinct); used from Step 8.
    __table_args__ = (UniqueConstraint("performed_by", "idempotency_key"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    type: Mapped[MovementType] = mapped_column(_movement_type())
    performed_by: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="RESTRICT"))
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    # sha256 of (endpoint + canonical body): a replayed key must carry the same request.
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StockMovement(Base):
    __tablename__ = "stock_movement"
    __table_args__ = (
        CheckConstraint(f"quantity BETWEEN 1 AND {MAX_QUANTITY}", name="quantity_range"),
        # The DB itself rejects malformed movements, even if the service had a bug.
        CheckConstraint(
            "(type = 'IN' AND from_warehouse_id IS NULL AND to_warehouse_id IS NOT NULL)"
            " OR (type = 'OUT' AND from_warehouse_id IS NOT NULL AND to_warehouse_id IS NULL)"
            " OR (type = 'TRANSFER' AND from_warehouse_id IS NOT NULL"
            " AND to_warehouse_id IS NOT NULL AND from_warehouse_id <> to_warehouse_id)",
            name="movement_shape",
        ),
        # Stock of a product in a warehouse = SUM over these two access paths.
        Index(None, "product_id", "from_warehouse_id"),
        Index(None, "product_id", "to_warehouse_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    operation_id: Mapped[int] = mapped_column(
        ForeignKey("operation.id", ondelete="RESTRICT"), index=True
    )
    type: Mapped[MovementType] = mapped_column(_movement_type())
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id", ondelete="RESTRICT"))
    quantity: Mapped[int]
    from_warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouse.id", ondelete="RESTRICT"), index=True
    )
    to_warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouse.id", ondelete="RESTRICT"), index=True
    )
    performed_by: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
