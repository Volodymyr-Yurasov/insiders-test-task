from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from src.schemas import CENTS, CustomModel, DbId, Name, PatchModel, strip_upper

Sku = Annotated[
    str,
    BeforeValidator(strip_upper),
    StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9._-]{0,63}$"),
]
# Exact money: Decimal in, Numeric(12,2) in the DB, a JSON string out (D10). Quantized after
# validation so the value the ORM holds (and echoes back) is always "15.00", never "15",
# matching what the DB returns on read.
Price = Annotated[
    Decimal,
    Field(ge=0, max_digits=12, decimal_places=2),
    AfterValidator(lambda price: price.quantize(CENTS)),
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


class ProductListItem(ProductRead):
    # Total across warehouses, or the stock in `warehouse_id` when that filter is given.
    stock: int


class ProductListQuery(CustomModel):
    """Query parameters of GET /products, validated before any SQL runs."""

    search: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    min_price: Annotated[Decimal | None, Field(ge=0)] = None
    max_price: Annotated[Decimal | None, Field(ge=0)] = None
    min_stock: Annotated[int | None, Field(ge=0)] = None
    max_stock: Annotated[int | None, Field(ge=0)] = None
    below_min_stock: bool = False
    warehouse_id: DbId | None = None
    # Literal whitelist: the sort key is never interpolated into SQL.
    sort: Literal["id", "sku", "name", "price", "stock"] = "id"
    order: Literal["asc", "desc"] = "asc"

    @model_validator(mode="after")
    def _ranges(self) -> Self:
        for low, high in (("min_price", "max_price"), ("min_stock", "max_stock")):
            low_value, high_value = getattr(self, low), getattr(self, high)
            if low_value is not None and high_value is not None and low_value > high_value:
                raise ValueError(f"{low} must not exceed {high}")
        return self


class WarehouseStock(CustomModel):
    warehouse_id: int
    location_code: str
    quantity: int


class ProductDetail(ProductRead):
    total_stock: int
    stock_by_warehouse: list[WarehouseStock]
