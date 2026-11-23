from decimal import Decimal

from sqlalchemy import CheckConstraint, Identity, Numeric, String, false
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base, TimestampMixin


class Product(TimestampMixin, Base):
    __tablename__ = "product"
    __table_args__ = (
        CheckConstraint("base_price >= 0", name="base_price_non_negative"),
        CheckConstraint("min_stock_level >= 0", name="min_stock_level_non_negative"),
    )

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    # Unique across soft-deleted rows too (D9): a deleted SKU can't silently come back.
    sku: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    base_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    min_stock_level: Mapped[int] = mapped_column(default=0, server_default="0")
    is_deleted: Mapped[bool] = mapped_column(default=False, server_default=false())
