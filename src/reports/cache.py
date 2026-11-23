"""Redis cache for the valuation report (spec 2.4), invalidated by a generation counter.

Keys:  valuation:gen          -> integer generation (missing = 0)
       valuation:v:{gen}      -> cached report JSON (TTL as a safety net)
       valuation:lock:{gen}   -> recompute lock (stampede guard)

Why a generation counter instead of DELETE: a slow reader may compute the report from
pre-commit data, then a movement commits and invalidates, then the reader stores its stale
result. With DELETE that stale value would be served until the next movement; here it lands
under an old generation that nobody reads again. Invalidation (INCR) happens strictly AFTER
the DB commit, and readers read the generation BEFORE querying the DB.

Redis is an optimization: every Redis failure degrades to computing from the DB.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Literal

from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.reports.config import reports_settings
from src.reports.schemas import ValuationReport

logger = logging.getLogger(__name__)

GENERATION_KEY = "valuation:gen"
CacheStatus = Literal["HIT", "MISS", "BYPASS"]


def value_key(generation: int) -> str:
    return f"valuation:v:{generation}"


def lock_key(generation: int) -> str:
    return f"valuation:lock:{generation}"


async def current_generation(redis: Redis) -> int:
    return int(await redis.get(GENERATION_KEY) or 0)


async def get_or_compute(
    redis: Redis, compute: Callable[[], Awaitable[ValuationReport]]
) -> tuple[ValuationReport, CacheStatus]:
    try:
        generation = await current_generation(redis)
        cached = await redis.get(value_key(generation))
        if cached is not None:
            return ValuationReport.model_validate_json(cached), "HIT"

        # Stampede guard: one request recomputes, the others briefly wait for its result.
        if not await redis.set(
            lock_key(generation), "1", nx=True, ex=reports_settings.VALUATION_LOCK_TTL_S
        ):
            waited = 0.0
            while waited < reports_settings.VALUATION_LOCK_WAIT_S:
                await asyncio.sleep(reports_settings.VALUATION_LOCK_POLL_S)
                waited += reports_settings.VALUATION_LOCK_POLL_S
                cached = await redis.get(value_key(generation))
                if cached is not None:
                    return ValuationReport.model_validate_json(cached), "HIT"
            # The winner is slow or died: answer from the DB without caching.
            return await compute(), "MISS"

        try:
            report = await compute()
            await store(redis, generation, report)
        finally:
            await redis.delete(lock_key(generation))
        return report, "MISS"
    except RedisError:
        logger.warning("valuation cache unavailable; serving from the database", exc_info=True)
        return await compute(), "BYPASS"


async def store(redis: Redis, generation: int, report: ValuationReport) -> None:
    await redis.set(
        value_key(generation),
        report.model_dump_json(),
        ex=reports_settings.VALUATION_CACHE_TTL_S,
    )


async def invalidate(redis: Redis) -> None:
    """Call AFTER the write committed. Never fails the request: the TTL bounds staleness."""
    try:
        await redis.incr(GENERATION_KEY)
    except RedisError:
        logger.error("valuation cache invalidation failed; stale for up to the TTL", exc_info=True)
