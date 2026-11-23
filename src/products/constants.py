from enum import StrEnum


class ErrorCode(StrEnum):
    PRODUCT_NOT_FOUND = "product_not_found"
    SKU_TAKEN = "sku_taken"
    PRODUCT_HAS_STOCK = "product_has_stock"
