from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any, ClassVar

from fastapi import Depends, Request
from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.config import Config

# Postgres-native style suffixes (fastapi-best-practices). `_N_` tokens name multi-column
# indexes/uniques after ALL their columns, e.g. operation_performed_by_idempotency_key_key.
NAMING_CONVENTION = {
    "ix": "%(table_name)s_%(column_0_N_name)s_idx",
    "uq": "%(table_name)s_%(column_0_N_name)s_key",
    "ck": "%(table_name)s_%(constraint_name)s_check",
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
    "pk": "%(table_name)s_pkey",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """created_at/updated_at for every mutable table (append-only ledgers use created_at only).

    eager_defaults: fetch server-generated values (now()) via RETURNING on INSERT *and* UPDATE,
    so reading them after a flush never triggers a lazy load (which async sessions forbid).
    """

    __mapper_args__: ClassVar[dict[str, Any]] = {"eager_defaults": True}

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


def create_engine(config: Config) -> AsyncEngine:
    return create_async_engine(
        str(config.DATABASE_URL),
        pool_size=config.DB_POOL_SIZE,
        max_overflow=config.DB_MAX_OVERFLOW,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_session)]
