from typing import Annotated

from fastapi import APIRouter, Depends, Response
from redis.asyncio import Redis

from src.auth.dependencies import admin_user
from src.database import DbSession
from src.redis import get_redis
from src.reports import cache
from src.reports import service as reports_service
from src.reports.schemas import ValuationReport

# The financial report is admin-only (spec 2.6): enforced for the whole router.
router = APIRouter(prefix="/reports", tags=["reports"], dependencies=[Depends(admin_user)])


@router.get(
    "/valuation",
    response_model=ValuationReport,
    summary="Stock value per warehouse (stock * current base price) and grand total (admin)",
    description=(
        "Cached in Redis and invalidated on every committed movement and catalog change. "
        "`X-Cache` says HIT, MISS or BYPASS (Redis unavailable); `generated_at` shows when "
        "the numbers were computed."
    ),
)
async def get_valuation(
    response: Response, db: DbSession, redis: Annotated[Redis, Depends(get_redis)]
) -> ValuationReport:
    report, status = await cache.get_or_compute(
        redis, lambda: reports_service.compute_valuation(db)
    )
    response.headers["X-Cache"] = status
    return report
