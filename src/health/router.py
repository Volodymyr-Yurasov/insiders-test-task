import asyncio
import logging
from collections.abc import Awaitable
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from src.config import settings
from src.health.schemas import ComponentStatus, HealthResponse
from src.redis import get_redis

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


async def _probe(name: str, check: Awaitable[object], limit_s: float) -> ComponentStatus:
    try:
        async with asyncio.timeout(limit_s):
            await check
    except Exception:
        logger.warning("health check failed: %s", name, exc_info=True)
        return "error"
    return "ok"


async def _ping_db(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness/readiness probe for DB and Redis",
    responses={503: {"model": HealthResponse, "description": "A dependency is unreachable"}},
)
async def health(
    request: Request,
    response: Response,
    redis: Annotated[Redis, Depends(get_redis)],
) -> HealthResponse:
    limit_s = settings.HEALTH_CHECK_TIMEOUT_S
    db_status, redis_status = await asyncio.gather(
        _probe("db", _ping_db(request.app.state.engine), limit_s),
        _probe("redis", redis.ping(), limit_s),
    )
    overall: ComponentStatus = "ok" if db_status == redis_status == "ok" else "error"
    if overall != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(status=overall, db=db_status, redis=redis_status)
