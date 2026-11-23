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
import uuid
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
    """compute() runs at most ONCE per call: Redis failures around it are handled separately,
    so they never trigger a second (expensive) computation or mask a DB error."""
    lock_token = uuid.uuid4().hex
    try:
        generation = await current_generation(redis)
        cached = await redis.get(value_key(generation))
        if cached is not None:
            return ValuationReport.model_validate_json(cached), "HIT"
        # Stampede guard: one request recomputes, the others briefly wait for its result.
        won_lock = await redis.set(
            lock_key(generation), lock_token, nx=True, ex=reports_settings.VALUATION_LOCK_TTL_S
        )
    except RedisError:
        logger.warning("valuation cache unavailable; serving from the database", exc_info=True)
        return await compute(), "BYPASS"

    if not won_lock:
        cached = await _wait_for_winner(redis, generation)
        if cached is not None:
            return ValuationReport.model_validate_json(cached), "HIT"
        # The winner is slow or died: answer from the DB without caching.
        return await compute(), "MISS"

    try:
        report = await compute()
        # Store BEFORE releasing the lock: otherwise a request arriving in between sees
        # neither the value nor the lock and recomputes (stampede guard defeated).
        try:
            await store(redis, generation, report)
        except RedisError:
            logger.warning("could not cache the valuation report", exc_info=True)
    finally:
        await _release_lock(redis, generation, lock_token)
    return report, "MISS"


async def _wait_for_winner(redis: Redis, generation: int) -> bytes | str | None:
    waited = 0.0
    try:
        while waited < reports_settings.VALUATION_LOCK_WAIT_S:
            await asyncio.sleep(reports_settings.VALUATION_LOCK_POLL_S)
            waited += reports_settings.VALUATION_LOCK_POLL_S
            cached: bytes | str | None = await redis.get(value_key(generation))
            if cached is not None:
                return cached
    except RedisError:
        logger.warning("valuation cache unavailable while waiting", exc_info=True)
    return None


# Delete the lock only if it's still ours: if compute() outlived the lock TTL, the key may
# already belong to another request, and deleting it would re-open the stampede.
_RELEASE_IF_OWNER = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


async def _release_lock(redis: Redis, generation: int, token: str) -> None:
    try:
        await redis.eval(_RELEASE_IF_OWNER, 1, lock_key(generation), token)
    except RedisError:
        logger.warning("could not release the valuation lock (expires on its own)", exc_info=True)


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
