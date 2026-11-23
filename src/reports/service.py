from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Numeric, Select, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.movements.models import StockMovement as SM
from src.products.models import Product
from src.reports.schemas import ValuationReport, WarehouseValuation
from src.warehouses.models import Warehouse

_CENTS = Decimal("0.01")


def valuation_query() -> Select[int, str, str, bool, int, Decimal]:
    """Value of every warehouse in ONE statement (spec 2.3).

    Σ(signed movement qty * current base price) = Σ(stock * price), since the price is per
    product: no intermediate per-product stock step. LEFT JOINs keep warehouses that have no
    movements (an inner product join would silently drop them); COALESCE turns their NULL
    sums into 0, and numeric(20,2) renders "0.00" and can't overflow numeric(12,2).
    """
    signed_quantity = case((SM.to_warehouse_id == Warehouse.id, SM.quantity), else_=-SM.quantity)
    total_units = func.coalesce(func.sum(signed_quantity), 0).cast(BigInteger)
    total_value = func.coalesce(func.sum(signed_quantity * Product.base_price), 0).cast(
        Numeric(20, 2)
    )
    return (
        select(
            Warehouse.id,
            Warehouse.name,
            Warehouse.location_code,
            Warehouse.is_active,
            total_units.label("total_units"),
            total_value.label("total_value"),
        )
        .outerjoin(
            SM, or_(SM.from_warehouse_id == Warehouse.id, SM.to_warehouse_id == Warehouse.id)
        )
        .outerjoin(Product, Product.id == SM.product_id)
        .group_by(Warehouse.id)
        .order_by(Warehouse.id)
    )


async def compute_valuation(db: AsyncSession) -> ValuationReport:
    # Step 6: served from the Redis cache; this is the cache-miss path.
    rows = (await db.execute(valuation_query())).all()
    warehouses = [
        WarehouseValuation(
            warehouse_id=warehouse_id,
            name=name,
            location_code=location_code,
            is_active=is_active,
            total_units=units,
            total_value=value,
        )
        for warehouse_id, name, location_code, is_active, units, value in rows
    ]
    grand_total = sum((w.total_value for w in warehouses), Decimal(0)).quantize(_CENTS)
    return ValuationReport(
        warehouses=warehouses, grand_total=grand_total, generated_at=datetime.now(UTC)
    )
