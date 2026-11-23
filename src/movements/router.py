from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, status

from src.auth.dependencies import CurrentUser, current_user
from src.database import DbSession
from src.movements import service as movements_service
from src.movements.constants import MovementType
from src.movements.schemas import (
    BatchCreate,
    MovementCreate,
    MovementHistoryItem,
    OperationRead,
)
from src.pagination import Page, Pagination
from src.reports.dependencies import ValuationCache

# Storekeepers and admins may both create movements (spec 2.6).
router = APIRouter(prefix="/movements", tags=["movements"], dependencies=[Depends(current_user)])

ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "Unknown warehouse, or unknown/deleted SKUs (all listed)"},
    409: {"description": "Insufficient stock (every shortage listed) or inactive warehouse"},
    503: {"description": "Lock wait timed out; retry after Retry-After seconds"},
}


@router.get(
    "",
    response_model=Page[MovementHistoryItem],
    summary="Movement history, newest first",
    description="`warehouse_id` matches movements into or out of it; `date_to` is exclusive.",
)
async def list_movements(
    db: DbSession,
    page: Pagination,
    product_id: int | None = None,
    warehouse_id: int | None = None,
    type: MovementType | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict[str, Any]:
    return await movements_service.list_movements(
        db,
        page,
        product_id=product_id,
        warehouse_id=warehouse_id,
        type_=type,
        date_from=date_from,
        date_to=date_to,
    )


@router.post(
    "",
    response_model=OperationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Record one movement (IN / OUT / TRANSFER)",
    responses=ERRORS,
)
async def create_movement(
    body: MovementCreate, user: CurrentUser, db: DbSession, valuation_cache: ValuationCache
) -> OperationRead:
    operation = await movements_service.execute(db, user.id, movements_service.Command.single(body))
    await valuation_cache.invalidate()  # strictly after the commit (spec 2.4)
    return operation


@router.post(
    "/batch",
    response_model=OperationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Record a batch atomically: all items or none",
    description=(
        "All items share the type and warehouses. If any item lacks stock, nothing is "
        "written and the 409 lists every shortage."
    ),
    responses=ERRORS,
)
async def create_batch(
    body: BatchCreate, user: CurrentUser, db: DbSession, valuation_cache: ValuationCache
) -> OperationRead:
    operation = await movements_service.execute(db, user.id, movements_service.Command.batch(body))
    await valuation_cache.invalidate()
    return operation
