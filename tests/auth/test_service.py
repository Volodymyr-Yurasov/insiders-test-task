import asyncio

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select, update

from src.auth import service as auth_service
from src.auth.config import auth_settings
from src.auth.constants import Role
from src.auth.exceptions import LastAdmin
from src.auth.models import AppUser
from src.auth.schemas import UserUpdate
from tests.conftest import CreateUser


async def test_last_admin_guard_waits_for_inflight_demotion(
    app: FastAPI, create_user: CreateUser, admin: AppUser
) -> None:
    """Deterministic interleaving of two admins demoting each other.

    Without the FOR UPDATE on the active-admin rows, the second transaction would still see
    the uncommitted first demotion's target as an admin (READ COMMITTED), conclude another
    admin remains, and leave the system with zero admins.
    """
    other = await create_user(Role.ADMIN)
    factory = app.state.session_factory
    async with factory() as first, factory() as second:
        # In-flight transaction #1: holds the admin-row locks and has demoted `other`,
        # but hasn't committed yet.
        await first.execute(
            select(AppUser.id)
            .where(AppUser.role == Role.ADMIN, AppUser.is_active)
            .order_by(AppUser.id)
            .with_for_update()
        )
        await first.execute(
            update(AppUser).where(AppUser.id == other.id).values(role=Role.STOREKEEPER)
        )

        racing = asyncio.create_task(
            auth_service.update_user(second, admin.id, UserUpdate(role=Role.STOREKEEPER))
        )
        await asyncio.sleep(0.3)
        assert not racing.done(), "second demotion must block on the admin-row lock"

        await first.commit()
        with pytest.raises(LastAdmin):
            await racing


async def test_seed_first_admin_is_idempotent(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth_settings, "FIRST_ADMIN_USERNAME", "Root")
    monkeypatch.setattr(auth_settings, "FIRST_ADMIN_PASSWORD", SecretStr("original-pass"))
    async with app.state.session_factory() as db:
        await auth_service.ensure_first_admin(db)

        # A restart with a changed env password must NOT overwrite the existing admin.
        monkeypatch.setattr(auth_settings, "FIRST_ADMIN_PASSWORD", SecretStr("changed-pass"))
        await auth_service.ensure_first_admin(db)

        admins = (await db.scalars(select(AppUser).where(AppUser.username == "root"))).all()
        assert [(a.username, a.role) for a in admins] == [("root", Role.ADMIN)]
        assert await auth_service.authenticate(db, "root", "original-pass") is not None
        assert await auth_service.authenticate(db, "root", "changed-pass") is None
