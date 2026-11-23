from datetime import datetime
from typing import Annotated

from pydantic import BeforeValidator, StringConstraints

from src.schemas import CustomModel, Name, PatchModel, strip_upper

LocationCode = Annotated[
    str,
    BeforeValidator(strip_upper),
    StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9_-]{0,31}$"),
]
Address = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class WarehouseCreate(CustomModel):
    name: Name
    location_code: LocationCode
    address: Address
    is_active: bool = True


class WarehouseUpdate(PatchModel):
    name: Name | None = None
    location_code: LocationCode | None = None
    address: Address | None = None
    is_active: bool | None = None


class WarehouseStockItem(CustomModel):
    product_id: int
    sku: str
    name: str
    quantity: int


class WarehouseRead(CustomModel):
    id: int
    name: str
    location_code: str
    address: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
