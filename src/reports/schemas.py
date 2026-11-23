from datetime import datetime
from decimal import Decimal

from src.schemas import CustomModel


class WarehouseValuation(CustomModel):
    warehouse_id: int
    name: str
    location_code: str
    is_active: bool
    total_units: int
    total_value: Decimal


class ValuationReport(CustomModel):
    warehouses: list[WarehouseValuation]
    grand_total: Decimal
    generated_at: datetime
