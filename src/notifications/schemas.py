from src.schemas import CustomModel


class LowStockAlert(CustomModel):
    """Plain data handed to a background task: no ORM objects, no session (it's closed by then)."""

    operation_id: int
    product_id: int
    sku: str
    product_name: str
    warehouse_id: int
    stock_before: int
    stock_after: int
    min_stock_level: int
