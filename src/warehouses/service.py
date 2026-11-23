from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.movements import stock as movements_stock
from src.movements.models import StockMovement
from src.pagination import PageParams, paginate
from src.products.models import Product
from src.warehouses.exceptions import LocationCodeTaken, WarehouseNotEmpty
from src.warehouses.models import Warehouse
from src.warehouses.schemas import WarehouseCreate, WarehouseUpdate


async def create_warehouse(db: AsyncSession, data: WarehouseCreate) -> Warehouse:
    # ON CONFLICT, not a pre-check (races) nor IntegrityError (aborts the transaction).
    warehouse = (
        await db.execute(
            insert(Warehouse)
            .values(**data.model_dump())
            .on_conflict_do_nothing(index_elements=[Warehouse.location_code])
            .returning(Warehouse)
        )
    ).scalar_one_or_none()
    if warehouse is None:
        raise LocationCodeTaken()
    await db.commit()
    # Step 6: invalidate valuation cache (a new warehouse appears in the report).
    return warehouse


async def list_warehouses(
    db: AsyncSession, page: PageParams, *, is_active: bool | None = None
) -> dict[str, Any]:
    stmt = select(Warehouse).order_by(Warehouse.id)
    if is_active is not None:
        stmt = stmt.where(Warehouse.is_active.is_(is_active))
    return await paginate(db, stmt, page)


async def update_warehouse(
    db: AsyncSession, warehouse: Warehouse, changes: WarehouseUpdate
) -> Warehouse:
    if changes.is_active is False:
        # D8: lock the row (conflicts with the movement engine's FOR SHARE, so no movement
        # can land in between), re-read it, then require the warehouse to be empty.
        await db.scalar(
            select(Warehouse)
            .where(Warehouse.id == warehouse.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if await movements_stock.warehouse_holds_stock(db, warehouse.id):
            raise WarehouseNotEmpty()
    for field, value in changes.changes().items():
        setattr(warehouse, field, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        # UPDATE can't use ON CONFLICT; location_code is the only unique column we change.
        await db.rollback()
        raise LocationCodeTaken() from exc
    return warehouse


async def list_stock(db: AsyncSession, warehouse_id: int, page: PageParams) -> dict[str, Any]:
    """Products currently in the warehouse (quantity > 0), from one grouped ledger query."""
    quantity = movements_stock.net_in_warehouse(warehouse_id)
    per_product = (
        select(StockMovement.product_id, quantity.label("quantity"))
        .where(movements_stock.touches_warehouse(warehouse_id))
        .group_by(StockMovement.product_id)
        .having(quantity > 0)
        .subquery("per_product")
    )
    stmt = (
        select(Product.id.label("product_id"), Product.sku, Product.name, per_product.c.quantity)
        .join(per_product, per_product.c.product_id == Product.id)
        .order_by(Product.sku, Product.id)
    )
    return await paginate(db, stmt, page)
