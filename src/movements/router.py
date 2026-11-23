from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from redis.asyncio import Redis

from src.auth.dependencies import CurrentUser, current_user
from src.database import DbSession
from src.idempotency import service as idempotency_service
from src.idempotency.constants import REPLAYED_HEADER
from src.idempotency.dependencies import IdempotencyKey
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
from src.notifications.service import Notifier
from src.pagination import Page, Pagination
from src.redis import get_redis
from src.reports.dependencies import ValuationCache

# Storekeepers and admins may both create movements (spec 2.6).
router = APIRouter(prefix="/movements", tags=["movements"], dependencies=[Depends(current_user)])

ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "Unknown warehouse, or unknown/deleted SKUs (all listed)"},
    409: {
        "description": "Insufficient stock (every shortage listed), inactive warehouse, "
        "or the same Idempotency-Key is still in flight"
    },
    422: {"description": "Invalid request, or Idempotency-Key reused with a different request"},
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


@dataclass(frozen=True)
class _WriteContext:
    """Everything a movement write needs around the engine call."""

    user_id: int
    db: DbSession
    redis: Redis
    valuation_cache: ValuationCache
    notifier: Notifier
    background_tasks: BackgroundTasks
    idempotency_key: str | None


async def _write_context(
    user: CurrentUser,
    db: DbSession,
    redis: Annotated[Redis, Depends(get_redis)],
    valuation_cache: ValuationCache,
    notifier: NotifierDep,
    background_tasks: BackgroundTasks,
    idempotency_key: IdempotencyKey = None,
) -> _WriteContext:
    return _WriteContext(
        user.id, db, redis, valuation_cache, notifier, background_tasks, idempotency_key
    )


WriteContext = Annotated[_WriteContext, Depends(_write_context)]


@router.post(
    "",
    response_model=OperationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Record one movement (IN / OUT / TRANSFER)",
    responses=ERRORS,
)
async def create_movement(body: MovementCreate, ctx: WriteContext) -> OperationRead | JSONResponse:
    return await _record(
        ctx,
        "movement",
        body,
        lambda key, fp: movements_service.Command.single(body, key=key, fingerprint=fp),
    )


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
async def create_batch(body: BatchCreate, ctx: WriteContext) -> OperationRead | JSONResponse:
    return await _record(
        ctx,
        "batch",
        body,
        lambda key, fp: movements_service.Command.batch(body, key=key, fingerprint=fp),
    )


def _replayed(status_code: int, body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(body, status_code=status_code, headers={REPLAYED_HEADER: "true"})


async def _record(
    ctx: _WriteContext,
    endpoint: str,
    body: BaseModel,
    build: Callable[[str | None, str | None], movements_service.Command],
) -> OperationRead | JSONResponse:
    """Idempotency check -> engine -> post-commit side effects.

    Errors raise before any side effect, so a rolled-back request neither invalidates the
    cache nor notifies, and its idempotency marker is released for a real retry.
    """
    key = ctx.idempotency_key
    request_fp = idempotency_service.fingerprint(endpoint, body) if key else None
    token: str | None = None
    if key is not None and request_fp is not None:
        decision = await idempotency_service.begin(ctx.redis, ctx.user_id, key, request_fp)
        if isinstance(decision, idempotency_service.Replay):
            return _replayed(decision.status_code, decision.body)
        token = decision.token

    try:
        result = await movements_service.execute(ctx.db, ctx.user_id, build(key, request_fp))
    except Exception:
        if key is not None and token is not None:
            await idempotency_service.release(ctx.redis, ctx.user_id, key, token)
        raise

    payload = result.operation.model_dump(mode="json")
    if key is not None and request_fp is not None and token is not None:
        await idempotency_service.complete(
            ctx.redis, ctx.user_id, key, request_fp, status.HTTP_201_CREATED, payload
        )
    if result.replayed:  # the DB already had this key's operation: nothing new was written
        return _replayed(status.HTTP_201_CREATED, payload)

    await ctx.valuation_cache.invalidate()  # strictly after the commit (spec 2.4)
    if result.low_stock:
        # Runs after the response is sent (spec 2.5: the HTTP response is not blocked).
        ctx.background_tasks.add_task(
            notifications_service.safe_send, ctx.notifier, result.low_stock
        )
    return result.operation
