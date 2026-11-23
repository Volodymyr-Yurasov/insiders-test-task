from src.exceptions import Conflict, NotFound
from src.movements.constants import ErrorCode


class InsufficientStock(Conflict):
    # Raised with shortages=[{sku, requested, available, missing}, ...] listing EVERY short item.
    CODE = ErrorCode.INSUFFICIENT_STOCK
    DETAIL = "Insufficient stock in the source warehouse"


class ProductsNotFound(NotFound):
    # Raised with missing_skus=[...] listing every unknown or deleted SKU.
    CODE = ErrorCode.PRODUCTS_NOT_FOUND
    DETAIL = "Some products do not exist"


class WarehouseInactive(Conflict):
    CODE = ErrorCode.WAREHOUSE_INACTIVE
    DETAIL = "Warehouse is inactive"
