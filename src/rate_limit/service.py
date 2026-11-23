"""Sliding-window rate limit per employee (bonus spec), as an atomic Redis Lua script.

A sliding-window *log*: one ZSET per user whose members are the timestamps of accepted
transactions. Unlike fixed windows it has no boundary burst (100 requests across 12:00:59 /
12:01:00). Everything happens inside one script, so the check-and-add is atomic: two
concurrent requests can never both see "49" and both pass.
"""

import logging
import math
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.rate_limit.config import rate_limit_settings

logger = logging.getLogger(__name__)

# KEYS[1] = zset; ARGV = limit, window_ms, unique member.
# Redis TIME (not the API host clock): several instances with skewed clocks still agree.
_SLIDING_WINDOW = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window_ms)
local count = redis.call('ZCARD', key)
if count < limit then
    redis.call('ZADD', key, now, ARGV[3])
    redis.call('PEXPIRE', key, window_ms)
    return {1, limit - count - 1, 0}
end
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
return {0, 0, tonumber(oldest[2]) + window_ms - now}
"""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_s: int = 0
    # The ZSET member recorded for this request (None if denied or Redis was down);
    # lets a request that turns out to write nothing give its slot back.
    member: str | None = None


async def hit(redis: Redis, user_id: int) -> Decision:
    """Record one transaction attempt for the user if within the limit."""
    limit = rate_limit_settings.RATE_LIMIT_MAX_REQUESTS
    window_ms = int(rate_limit_settings.RATE_LIMIT_WINDOW_S * 1000)
    member = uuid.uuid4().hex  # unique: two requests in the same millisecond still count twice
    try:
        allowed, remaining, retry_after_ms = await redis.eval(
            _SLIDING_WINDOW, 1, _key(user_id), limit, window_ms, member
        )
    except RedisError:
        # Fail open: stock operations matter more than the limiter being available.
        logger.error("rate limiter unavailable; allowing the request", exc_info=True)
        return Decision(allowed=True, limit=limit, remaining=limit)
    if not allowed:
        retry_after_s = max(1, math.ceil(int(retry_after_ms) / 1000))
        return Decision(False, limit, 0, retry_after_s)
    return Decision(True, limit, int(remaining), member=member)


async def refund(redis: Redis, user_id: int, decision: Decision) -> None:
    """Give back the slot of a request that turned out to write nothing (idempotent replay)."""
    if decision.member is None:
        return
    try:
        await redis.zrem(_key(user_id), decision.member)
    except RedisError:
        logger.warning("could not refund rate-limit slot", exc_info=True)


def _key(user_id: int) -> str:
    return f"rl:{user_id}"
