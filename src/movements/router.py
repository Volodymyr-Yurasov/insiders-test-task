from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Response, status
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
from src.rate_limit import service as rate_limit_service
from src.rate_limit.exceptions import RateLimited
from src.redis import get_redis
from src.reports.dependencies import ValuationCache
from src.schemas import IdQuery

# Storekeepers and admins may both create movements (spec 2.6).
router = APIRouter(prefix="/movements", tags=["movements"], dependencies=[Depends(current_user)])

ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "Unknown warehouse, or unknown/deleted SKUs (all listed)"},
    429: {"description": "More than the allowed transactions per minute; see Retry-After"},
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
    product_id: IdQuery = None,
    warehouse_id: IdQuery = None,
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
    response: Response
    idempotency_key: str | None


async def _write_context(
    user: CurrentUser,
    db: DbSession,
    redis: Annotated[Redis, Depends(get_redis)],
    valuation_cache: ValuationCache,
    notifier: NotifierDep,
    background_tasks: BackgroundTasks,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> _WriteContext:
    return _WriteContext(
        user.id, db, redis, valuation_cache, notifier, background_tasks, response, idempotency_key
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


@dataclass(frozen=True)
class _Claim:
    """This request owns the Redis idempotency marker for `key`."""

    key: str
    fingerprint: str
    token: str


async def _release(ctx: _WriteContext, claim: _Claim | None) -> None:
    if claim is not None:
        await idempotency_service.release(ctx.redis, ctx.user_id, claim.key, claim.token)


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
    request_fp: str | None = None
    claim: _Claim | None = None  # set only while this request holds the Redis marker
    if key is not None:
        request_fp = idempotency_service.fingerprint(endpoint, body)
        decision = await idempotency_service.begin(ctx.redis, ctx.user_id, key, request_fp)
        if isinstance(decision, idempotency_service.Replay):
            return _replayed(decision.status_code, decision.body)
        if decision.token is not None:
            claim = _Claim(key, request_fp, decision.token)

    # From the moment the marker is claimed until the engine has committed, ANY way out
    # (429, business error, unexpected exception, CancelledError when the client is gone)
    # releases it, so a retry with the same key is never stuck behind "in progress".
    try:
        # Rate limit AFTER the replay check (replays write nothing and cost no quota) and
        # BEFORE the engine (failed business attempts still count: it protects the system).
        limit = await rate_limit_service.hit(ctx.redis, ctx.user_id)
        if not limit.allowed:
            raise RateLimited(limit.retry_after_s)
        result = await movements_service.execute(ctx.db, ctx.user_id, build(key, request_fp))
    except BaseException:
        await _release(ctx, claim)
        raise

    payload = result.operation.model_dump(mode="json")
    if claim is not None:
        await idempotency_service.complete(
            ctx.redis, ctx.user_id, claim.key, claim.fingerprint, status.HTTP_201_CREATED, payload
        )
    if result.replayed:
        # The DB already had this key's operation (Redis record expired or unavailable):
        # nothing was written, so the attempt gives its rate-limit slot back.
        await rate_limit_service.refund(ctx.redis, limit)
        return _replayed(status.HTTP_201_CREATED, payload)

    ctx.response.headers["X-RateLimit-Limit"] = str(limit.limit)
    ctx.response.headers["X-RateLimit-Remaining"] = str(limit.remaining)

    await ctx.valuation_cache.invalidate()  # strictly after the commit (spec 2.4)
    if result.low_stock:
        # Runs after the response is sent (spec 2.5: the HTTP response is not blocked).
        ctx.background_tasks.add_task(
            notifications_service.safe_send, ctx.notifier, result.low_stock
        )
    return result.operation
