from fastapi import Request
from redis.asyncio import Redis

from src.config import Config


def create_redis(config: Config) -> Redis:
    return Redis.from_url(str(config.REDIS_URL), decode_responses=True)


async def get_redis(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis
