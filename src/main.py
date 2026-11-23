import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from src.auth import router as auth_router
from src.auth import service as auth_service
from src.config import settings
from src.database import create_engine, create_session_factory
from src.exceptions import register_exception_handlers
from src.health import router as health_router
from src.movements import router as movements_router
from src.products import router as products_router
from src.redis import create_redis
from src.reports import router as reports_router
from src.warehouses import router as warehouses_router

# Application loggers (e.g. seed, notifications) at INFO; no-op if logging is already set up.
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s - %(message)s")


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with app.state.session_factory() as db:
            await auth_service.ensure_first_admin(db)
        yield
        await app.state.redis.aclose()
        await app.state.engine.dispose()

    app_configs: dict[str, Any] = {"title": "Inventory Management Engine API"}
    if not settings.ENVIRONMENT.shows_docs:
        # Disables /openapi.json and, with it, /docs and /redoc.
        app_configs["openapi_url"] = None

    app = FastAPI(**app_configs, lifespan=lifespan)

    # Created eagerly (no connections are opened yet) so the app is fully wired even when
    # the lifespan does not run, e.g. under httpx.ASGITransport in tests.
    engine = create_engine(settings)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.redis = create_redis(settings)

    register_exception_handlers(app)
    app.include_router(health_router.router)
    app.include_router(auth_router.router)
    app.include_router(auth_router.users_router)
    app.include_router(warehouses_router.router)
    app.include_router(products_router.router)
    app.include_router(movements_router.router)
    app.include_router(reports_router.router)
    return app


app = create_app()
