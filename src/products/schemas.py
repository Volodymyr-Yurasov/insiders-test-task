from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BeforeValidator, ConfigDict, Field, StringConstraints

from src.schemas import CustomModel, PatchModel


def _normalize_sku(value: object) -> object:
    return value.strip().upper() if isinstance(value, str) else value


Sku = Annotated[
    str,
    BeforeValidator(_normalize_sku),
    StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9._-]{0,63}$"),
]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
_CENTS = Decimal("0.01")

# Exact money: Decimal in, Numeric(12,2) in the DB, a JSON string out (D10). Quantized after
# validation so the value the ORM holds (and echoes back) is always "15.00", never "15",
# matching what the DB returns on read.
Price = Annotated[
    Decimal,
    Field(ge=0, max_digits=12, decimal_places=2),
    AfterValidator(lambda price: price.quantize(_CENTS)),
]
MinStockLevel = Annotated[int, Field(ge=0, le=1_000_000_000)]


class ProductCreate(CustomModel):
    sku: Sku
    name: Name
    base_price: Price
    min_stock_level: MinStockLevel = 0


class ProductUpdate(PatchModel):
    # extra="forbid": the SKU is immutable, so sending it is an error, not a silent no-op.
    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    base_price: Price | None = None
    min_stock_level: MinStockLevel | None = None


class ProductRead(CustomModel):
    id: int
    sku: str
    name: str
    base_price: Decimal
    min_stock_level: int
    created_at: datetime
    updated_at: datetime


class WarehouseStock(CustomModel):
    warehouse_id: int
    location_code: str
    quantity: int


class ProductDetail(ProductRead):
    total_stock: int
    stock_by_warehouse: list[WarehouseStock]
