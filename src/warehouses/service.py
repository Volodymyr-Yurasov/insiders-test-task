from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.pagination import PageParams, paginate
from src.warehouses.exceptions import LocationCodeTaken
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
    # Step 4 adds the D8 guard here: deactivation only when the warehouse holds no stock.
    for field, value in changes.changes().items():
        setattr(warehouse, field, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        # UPDATE can't use ON CONFLICT; location_code is the only unique column we change.
        await db.rollback()
        raise LocationCodeTaken() from exc
    return warehouse
