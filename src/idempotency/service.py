"""Idempotency-Key, Redis layer (fast path + 24 h result cache).

The guarantee itself lives in Postgres (UNIQUE (performed_by, idempotency_key) claimed with
INSERT ... ON CONFLICT DO NOTHING as the operation's first write). This layer answers
duplicates without touching the DB, reports in-flight duplicates immediately (409 instead of
blocking on the unique index), and replays results for 24 hours. Every Redis failure falls
back to "proceed" and lets the DB layer decide.
"""

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.idempotency.config import idempotency_settings
from src.idempotency.exceptions import IdempotencyInProgress, IdempotencyKeyReused

logger = logging.getLogger(__name__)

# Delete only if the marker is still ours (an expired marker may now belong to a retry).
_RELEASE_IF_OWNER = """
local value = redis.call('GET', KEYS[1])
if value and cjson.decode(value)['token'] == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


@dataclass(frozen=True)
class Proceed:
    # None when Redis is unavailable: no marker to complete or release.
    token: str | None


@dataclass(frozen=True)
class Replay:
    status_code: int
    body: dict[str, Any]


def fingerprint(endpoint: str, body: BaseModel) -> str:
    """Identity of a request: endpoint + canonical JSON of the *validated* body, so formatting
    differences ("p-1" vs "P-1", key order) don't matter but any semantic change does."""
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{endpoint}\n{canonical}".encode()).hexdigest()


def _redis_key(user_id: int, key: str) -> str:
    # Scoped per user: two users can never collide on (or probe) each other's keys.
    return f"idem:{user_id}:{key}"


async def begin(redis: Redis, user_id: int, key: str, request_fp: str) -> Proceed | Replay:
    redis_key = _redis_key(user_id, key)
    token = uuid.uuid4().hex
    marker = json.dumps({"state": "processing", "fp": request_fp, "token": token})
    try:
        for _ in range(2):  # a second pass covers a marker expiring between SET NX and GET
            if await redis.set(
                redis_key, marker, nx=True, ex=idempotency_settings.IDEMPOTENCY_PROCESSING_TTL_S
            ):
                return Proceed(token)
            raw = await redis.get(redis_key)
            if raw is None:
                continue
            try:
                existing = json.loads(raw)
                stored_fp, state = existing["fp"], existing["state"]
                replay = (
                    Replay(existing["status_code"], existing["body"]) if state == "done" else None
                )
            except (ValueError, KeyError, TypeError):
                # Corrupt or foreign record: don't 500, let the DB constraint decide.
                logger.warning("unreadable idempotency record for %s; using the DB", redis_key)
                return Proceed(None)
            if stored_fp != request_fp:
                raise IdempotencyKeyReused()
            if replay is None:
                raise IdempotencyInProgress()
            return replay
    except RedisError:
        logger.warning("idempotency cache unavailable; relying on the DB constraint", exc_info=True)
    return Proceed(None)


async def complete(
    redis: Redis, user_id: int, key: str, request_fp: str, status_code: int, body: dict[str, Any]
) -> None:
    record = {"state": "done", "fp": request_fp, "status_code": status_code, "body": body}
    try:
        await redis.set(
            _redis_key(user_id, key), json.dumps(record), ex=idempotency_settings.IDEMPOTENCY_TTL_S
        )
    except RedisError:
        logger.warning("could not cache idempotent result; the DB will replay it", exc_info=True)


async def release(redis: Redis, user_id: int, key: str, token: str) -> None:
    """After a failed request: nothing was written, so a retry with the same key may run."""
    try:
        await redis.eval(_RELEASE_IF_OWNER, 1, _redis_key(user_id, key), token)
    except RedisError:
        logger.warning("could not release idempotency marker (expires on its own)", exc_info=True)
