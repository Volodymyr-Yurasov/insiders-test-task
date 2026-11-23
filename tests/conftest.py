"""Shared test infrastructure.

Tests run against real Postgres + Redis (docker compose services), using a separate
`<db>_test` database and Redis DB 15. Isolation is done by TRUNCATE/FLUSHDB before each
test — deliberately NOT by wrapping each test in a rolled-back transaction, because that
would pin all requests to a single connection and make concurrency tests meaningless.
"""

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values
from sqlalchemy import make_url

ROOT = Path(__file__).resolve().parent.parent
TEST_REDIS_DB = 15

# --- Test environment: must be set BEFORE anything imports `src` (settings load at import).
# Derived from the dev config (env vars override .env) so host/port/credentials follow it.
_dev = {**dotenv_values(ROOT / ".env"), **os.environ}
_dev_db = make_url(
    _dev.get("DATABASE_URL") or "postgresql://inventory:inventory@localhost:5432/inventory"
)
TEST_DATABASE_URL = _dev_db.set(
    drivername="postgresql+asyncpg", database=f"{_dev_db.database}_test"
)
_dev_redis = urlsplit(_dev.get("REDIS_URL") or "redis://localhost:6379/0")
os.environ["DATABASE_URL"] = TEST_DATABASE_URL.render_as_string(hide_password=False)
os.environ["REDIS_URL"] = urlunsplit(_dev_redis._replace(path=f"/{TEST_REDIS_DB}"))
os.environ["ENVIRONMENT"] = "local"
os.environ["JWT_SECRET"] = "test-secret-not-for-production-0123456789abcdef"

import asyncio  # noqa: E402
from collections.abc import AsyncIterator, Awaitable, Callable  # noqa: E402
from uuid import uuid4  # noqa: E402

import asyncpg  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config as AlembicConfig  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from src.auth import service as auth_service  # noqa: E402
from src.auth import utils as auth_utils  # noqa: E402
from src.auth.constants import Role  # noqa: E402
from src.auth.models import AppUser  # noqa: E402
from src.main import create_app  # noqa: E402
from src.models import Base  # noqa: E402


async def _ensure_database() -> None:
    maintenance_dsn = TEST_DATABASE_URL.set(
        drivername="postgresql", database="postgres"
    ).render_as_string(hide_password=False)
    conn = await asyncpg.connect(maintenance_dsn)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", TEST_DATABASE_URL.database
        )
        if not exists:
            # Identifier comes from our own config, not user input.
            await conn.execute(f'CREATE DATABASE "{TEST_DATABASE_URL.database}"')
    finally:
        await conn.close()


def _alembic_upgrade() -> None:
    cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
async def app() -> AsyncIterator[FastAPI]:
    await _ensure_database()
    # env.py calls asyncio.run(), which can't nest inside the running test loop.
    await asyncio.to_thread(_alembic_upgrade)

    application = create_app()
    yield application
    await application.state.redis.aclose()
    await application.state.engine.dispose()


@pytest.fixture(autouse=True)
async def _clean_state(app: FastAPI) -> None:
    tables = [t.name for t in Base.metadata.sorted_tables]
    if tables:
        async with app.state.engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    await app.state.redis.flushdb()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# --- Auth fixtures (users are created through the service; tokens are minted directly,
# so tests don't pay an argon2 login per request).

TEST_PASSWORD = "password123"
CreateUser = Callable[..., Awaitable[AppUser]]


@pytest.fixture
def create_user(app: FastAPI) -> CreateUser:
    async def _create(
        role: Role = Role.STOREKEEPER,
        *,
        username: str | None = None,
        password: str = TEST_PASSWORD,
        is_active: bool = True,
    ) -> AppUser:
        async with app.state.session_factory() as db:
            user = await auth_service.create_user(
                db,
                username=username or f"{role}-{uuid4().hex[:8]}",
                password=password,
                role=role,
                is_active=is_active,
            )
        assert user is not None
        return user

    return _create


def auth_headers(user: AppUser) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth_utils.create_access_token(user.id)}"}


@pytest.fixture
async def admin(create_user: CreateUser) -> AppUser:
    return await create_user(Role.ADMIN)


@pytest.fixture
async def storekeeper(create_user: CreateUser) -> AppUser:
    return await create_user(Role.STOREKEEPER)


@pytest.fixture
def admin_headers(admin: AppUser) -> dict[str, str]:
    return auth_headers(admin)


@pytest.fixture
def storekeeper_headers(storekeeper: AppUser) -> dict[str, str]:
    return auth_headers(storekeeper)
