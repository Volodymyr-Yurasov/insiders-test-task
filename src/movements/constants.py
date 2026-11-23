from enum import StrEnum

MAX_QUANTITY = 1_000_000
MAX_BATCH_ITEMS = 100


class MovementType(StrEnum):
    IN = "IN"
    OUT = "OUT"
    TRANSFER = "TRANSFER"

    @property
    def decreases_source(self) -> bool:
        return self in (MovementType.OUT, MovementType.TRANSFER)


class ErrorCode(StrEnum):
    INSUFFICIENT_STOCK = "insufficient_stock"
    PRODUCTS_NOT_FOUND = "products_not_found"
    WAREHOUSE_INACTIVE = "warehouse_inactive"
