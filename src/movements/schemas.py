from datetime import datetime
from typing import Annotated, Self

from pydantic import Field, model_validator

from src.movements.constants import MAX_BATCH_ITEMS, MAX_QUANTITY, MovementType
from src.products.schemas import Sku
from src.schemas import CustomModel, DbId

Quantity = Annotated[int, Field(ge=1, le=MAX_QUANTITY)]


class _Route(CustomModel):
    """Which warehouses a movement touches; the allowed shape depends on the type."""

    type: MovementType
    from_warehouse_id: DbId | None = None
    to_warehouse_id: DbId | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        has_from, has_to = self.from_warehouse_id is not None, self.to_warehouse_id is not None
        match self.type:
            case MovementType.IN if has_from or not has_to:
                raise ValueError("IN needs to_warehouse_id and no from_warehouse_id")
            case MovementType.OUT if not has_from or has_to:
                raise ValueError("OUT needs from_warehouse_id and no to_warehouse_id")
            case MovementType.TRANSFER if not (has_from and has_to):
                raise ValueError("TRANSFER needs both from_warehouse_id and to_warehouse_id")
            case MovementType.TRANSFER if self.from_warehouse_id == self.to_warehouse_id:
                raise ValueError("TRANSFER source and destination must differ")
        return self


class MovementCreate(_Route):
    sku: Sku
    quantity: Quantity


class BatchItem(CustomModel):
    sku: Sku
    quantity: Quantity


class BatchCreate(_Route):
    items: Annotated[list[BatchItem], Field(min_length=1, max_length=MAX_BATCH_ITEMS)]

    @model_validator(mode="after")
    def _unique_skus(self) -> Self:
        # D5: [A:5, A:5] with stock 8 would pass two separate checks; reject, don't merge.
        skus = [item.sku for item in self.items]
        duplicates = sorted({sku for sku in skus if skus.count(sku) > 1})
        if duplicates:
            raise ValueError(f"Duplicate SKUs in batch: {', '.join(duplicates)}")
        return self


class MovementRead(CustomModel):
    id: int
    product_id: int
    sku: str
    quantity: int
    from_warehouse_id: int | None
    to_warehouse_id: int | None


class MovementHistoryItem(CustomModel):
    id: int
    operation_id: int
    type: MovementType
    product_id: int
    sku: str
    quantity: int
    from_warehouse_id: int | None
    to_warehouse_id: int | None
    performed_by: int
    created_at: datetime


class OperationRead(CustomModel):
    id: int
    type: MovementType
    performed_by: int
    created_at: datetime
    movements: list[MovementRead]
