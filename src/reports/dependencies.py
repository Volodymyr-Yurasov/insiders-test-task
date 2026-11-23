from typing import Annotated

from fastapi import Depends
from redis.asyncio import Redis

from src.redis import get_redis
from src.reports import cache


class _ValuationCache:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def invalidate(self) -> None:
        await cache.invalidate(self._redis)


async def valuation_cache(redis: Annotated[Redis, Depends(get_redis)]) -> _ValuationCache:
    return _ValuationCache(redis)


# Write routes call `await valuation_cache.invalidate()` after their service committed;
# errors raise before that line, so a rolled-back request never invalidates.
ValuationCache = Annotated[_ValuationCache, Depends(valuation_cache)]
