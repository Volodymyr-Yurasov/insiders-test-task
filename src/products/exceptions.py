from src.exceptions import Conflict, NotFound
from src.products.constants import ErrorCode


class ProductNotFound(NotFound):
    CODE = ErrorCode.PRODUCT_NOT_FOUND
    DETAIL = "Product not found"


class ProductHasStock(Conflict):
    CODE = ErrorCode.PRODUCT_HAS_STOCK
    DETAIL = "Product still has stock; write it off before deleting"


class SkuTaken(Conflict):
    # SKUs stay reserved after soft delete (D9), so say so: the "missing" product still owns it.
    CODE = ErrorCode.SKU_TAKEN
    DETAIL = "SKU already exists (possibly on a deleted product)"
