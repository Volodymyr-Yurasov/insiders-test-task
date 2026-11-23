import asyncio
import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from fastapi.concurrency import run_in_threadpool
from pwdlib import PasswordHash

from src.auth.config import auth_settings
from src.auth.exceptions import InvalidToken
from src.schemas import INT32_MAX

_hasher = PasswordHash.recommended()  # argon2id

# Verified against when the username doesn't exist, so the response takes as long as a real
# password check and timing doesn't reveal which usernames exist.
_DUMMY_HASH = _hasher.hash("timing-equalizer-not-a-real-password")


def normalize_username(username: str) -> str:
    return username.strip().lower()


# argon2 is deliberately CPU-heavy (~50 ms): keep it off the event loop, and cap how many
# run at once so a login flood can't occupy the whole shared threadpool.
_hash_slots = asyncio.Semaphore(auth_settings.PASSWORD_HASH_CONCURRENCY)


async def hash_password(password: str) -> str:
    async with _hash_slots:
        return await run_in_threadpool(_hasher.hash, password)


async def verify_password(password: str, password_hash: str) -> bool:
    async with _hash_slots:
        return await run_in_threadpool(_hasher.verify, password, password_hash)


async def burn_password_check(password: str) -> None:
    await verify_password(password, _DUMMY_HASH)


def new_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    # 256 bits of randomness: a fast hash is enough (no dictionary to attack, unlike passwords).
    return hashlib.sha256(token.encode()).hexdigest()


def decode_access_token(token: str) -> int:
    """Return the user id from a valid access token; raise InvalidToken otherwise."""
    try:
        claims = jwt.decode(
            token,
            auth_settings.JWT_SECRET.get_secret_value(),
            algorithms=[auth_settings.JWT_ALG],  # pinned: rejects alg=none / alg switching
            options={"require": ["exp", "iat", "sub"]},
        )
        user_id = int(claims["sub"])
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise InvalidToken() from exc
    if not 1 <= user_id <= INT32_MAX:  # never hand an out-of-range id to the DB driver
        raise InvalidToken()
    return user_id


def create_access_token(user_id: int, *, expires_delta: timedelta | None = None) -> str:
    now = datetime.now(UTC)
    expires_at = now + (expires_delta or timedelta(minutes=auth_settings.JWT_ACCESS_EXP_MINUTES))
    claims = {"sub": str(user_id), "iat": now, "exp": expires_at}
    return jwt.encode(
        claims, auth_settings.JWT_SECRET.get_secret_value(), algorithm=auth_settings.JWT_ALG
    )
