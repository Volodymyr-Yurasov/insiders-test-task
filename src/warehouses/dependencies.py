from typing import Annotated

from fastapi import Depends

from src.database import DbSession
from src.schemas import IdPath
from src.warehouses.exceptions import WarehouseNotFound
from src.warehouses.models import Warehouse


async def valid_warehouse_id(warehouse_id: IdPath, db: DbSession) -> Warehouse:
    warehouse = await db.get(Warehouse, warehouse_id)
    if warehouse is None:
        raise WarehouseNotFound()
    return warehouse


ValidWarehouse = Annotated[Warehouse, Depends(valid_warehouse_id)]
