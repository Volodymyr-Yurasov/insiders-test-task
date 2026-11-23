"""Stock derived from the ledger in SQL (spec rule: never stored as a static field)."""

from collections.abc import Collection

from sqlalchemy import ColumnElement, Integer, Subquery, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.movements.constants import MovementType
from src.movements.models import StockMovement as SM


def net_in_warehouse(warehouse_id: int) -> ColumnElement[int]:
    """Aggregate: arrivals into the warehouse minus departures from it."""
    inbound = func.coalesce(func.sum(SM.quantity).filter(SM.to_warehouse_id == warehouse_id), 0)
    outbound = func.coalesce(func.sum(SM.quantity).filter(SM.from_warehouse_id == warehouse_id), 0)
    return (inbound - outbound).cast(Integer)


def touches_warehouse(warehouse_id: int) -> ColumnElement[bool]:
    # OR over the two single-column indexes -> BitmapOr, reads only this warehouse's rows.
    return or_(SM.from_warehouse_id == warehouse_id, SM.to_warehouse_id == warehouse_id)


def signed_total() -> ColumnElement[int]:
    """Aggregate: total stock across all warehouses (TRANSFER nets to zero)."""
    signed = case(
        (SM.type == MovementType.IN, SM.quantity),
        (SM.type == MovementType.OUT, -SM.quantity),
        else_=0,
    )
    return func.coalesce(func.sum(signed), 0).cast(Integer)


def signed_ledger() -> Subquery:
    """One row per (product, warehouse, signed delta): +qty where it arrived, -qty where it left."""
    arrivals = select(
        SM.product_id, SM.to_warehouse_id.label("warehouse_id"), SM.quantity.label("delta")
    ).where(SM.to_warehouse_id.is_not(None))
    departures = select(
        SM.product_id, SM.from_warehouse_id.label("warehouse_id"), (-SM.quantity).label("delta")
    ).where(SM.from_warehouse_id.is_not(None))
    return arrivals.union_all(departures).subquery("signed_ledger")


async def warehouse_holds_stock(db: AsyncSession, warehouse_id: int) -> bool:
    per_product = (
        select(SM.product_id)
        .where(touches_warehouse(warehouse_id))
        .group_by(SM.product_id)
        .having(net_in_warehouse(warehouse_id) != 0)
    )
    return bool(await db.scalar(select(exists(per_product))))


async def product_total_stock(db: AsyncSession, product_id: int) -> int:
    total = await db.scalar(select(signed_total()).where(SM.product_id == product_id))
    return int(total or 0)


async def stock_in_warehouse(
    db: AsyncSession, warehouse_id: int, product_ids: Collection[int]
) -> dict[int, int]:
    """Current stock of each product in one warehouse, in ONE grouped query (0 if absent)."""
    rows = await db.execute(
        select(SM.product_id, net_in_warehouse(warehouse_id))
        .where(SM.product_id.in_(product_ids), touches_warehouse(warehouse_id))
        .group_by(SM.product_id)
    )
    stock = {product_id: 0 for product_id in product_ids}
    stock.update({product_id: quantity for product_id, quantity in rows})
    return stock
