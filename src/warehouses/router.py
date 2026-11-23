from typing import Any

from fastapi import APIRouter, Depends, status

from src.auth.dependencies import admin_user, current_user
from src.database import DbSession
from src.pagination import Page, Pagination
from src.warehouses import service as warehouses_service
from src.warehouses.dependencies import ValidWarehouse
from src.warehouses.models import Warehouse
from src.warehouses.schemas import (
    WarehouseCreate,
    WarehouseRead,
    WarehouseStockItem,
    WarehouseUpdate,
)

# Every route needs a logged-in user; write routes additionally require admin.
router = APIRouter(prefix="/warehouses", tags=["warehouses"], dependencies=[Depends(current_user)])
ADMIN_ONLY = [Depends(admin_user)]


@router.post(
    "",
    response_model=WarehouseRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=ADMIN_ONLY,
    summary="Create a warehouse (admin)",
    responses={409: {"description": "Location code already used"}},
)
async def create_warehouse(body: WarehouseCreate, db: DbSession) -> Warehouse:
    return await warehouses_service.create_warehouse(db, body)


@router.get("", response_model=Page[WarehouseRead], summary="List warehouses")
async def list_warehouses(
    db: DbSession, page: Pagination, is_active: bool | None = None
) -> dict[str, Any]:
    return await warehouses_service.list_warehouses(db, page, is_active=is_active)


@router.get(
    "/{warehouse_id}",
    response_model=WarehouseRead,
    summary="Get a warehouse",
    responses={404: {"description": "Warehouse not found"}},
)
async def get_warehouse(warehouse: ValidWarehouse) -> Warehouse:
    return warehouse


@router.get(
    "/{warehouse_id}/stock",
    response_model=Page[WarehouseStockItem],
    summary="Products currently in the warehouse (quantity > 0)",
    responses={404: {"description": "Warehouse not found"}},
)
async def get_warehouse_stock(
    warehouse: ValidWarehouse, db: DbSession, page: Pagination
) -> dict[str, Any]:
    return await warehouses_service.list_stock(db, warehouse.id, page)


@router.patch(
    "/{warehouse_id}",
    response_model=WarehouseRead,
    dependencies=ADMIN_ONLY,
    summary="Edit, deactivate or reactivate a warehouse (admin)",
    responses={
        404: {"description": "Warehouse not found"},
        409: {"description": "Location code already used, or deactivating a non-empty warehouse"},
    },
)
async def update_warehouse(
    warehouse: ValidWarehouse, body: WarehouseUpdate, db: DbSession
) -> Warehouse:
    return await warehouses_service.update_warehouse(db, warehouse, body)
