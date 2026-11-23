from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, status

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
from src.notifications import service as notifications_service
from src.notifications.dependencies import NotifierDep
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
    body: MovementCreate,
    user: CurrentUser,
    db: DbSession,
    valuation_cache: ValuationCache,
    notifier: NotifierDep,
    background_tasks: BackgroundTasks,
) -> OperationRead:
    command = movements_service.Command.single(body)
    return await _record(command, user.id, db, valuation_cache, notifier, background_tasks)


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
    body: BatchCreate,
    user: CurrentUser,
    db: DbSession,
    valuation_cache: ValuationCache,
    notifier: NotifierDep,
    background_tasks: BackgroundTasks,
) -> OperationRead:
    command = movements_service.Command.batch(body)
    return await _record(command, user.id, db, valuation_cache, notifier, background_tasks)


async def _record(
    command: movements_service.Command,
    user_id: int,
    db: DbSession,
    valuation_cache: ValuationCache,
    notifier: NotifierDep,
    background_tasks: BackgroundTasks,
) -> OperationRead:
    """Run the engine, then the post-commit side effects. Any error raises before this point
    returns, so a rolled-back request neither invalidates the cache nor notifies."""
    result = await movements_service.execute(db, user_id, command)
    await valuation_cache.invalidate()  # strictly after the commit (spec 2.4)
    if result.low_stock:
        # Runs after the response is sent (spec 2.5: the HTTP response is not blocked).
        background_tasks.add_task(notifications_service.safe_send, notifier, result.low_stock)
    return result.operation
