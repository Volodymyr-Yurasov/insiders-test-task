import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select, update

from src.auth import service as auth_service
from src.auth import utils as auth_utils
from src.auth.config import auth_settings
from src.auth.constants import Role
from src.auth.exceptions import LastAdmin
from src.auth.models import AppUser, RefreshToken
from src.auth.schemas import UserUpdate
from src.movements.config import movements_settings
from tests.conftest import TEST_PASSWORD, CreateUser, MakeProduct, MakeWarehouse


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


async def test_login_prunes_old_refresh_tokens(
    app: FastAPI, client: AsyncClient, create_user: CreateUser
) -> None:
    user = await create_user(username="pruner")
    now = datetime.now(UTC)
    async with app.state.session_factory() as db:
        for label, age_days in (("ancient", 30), ("recent", 1)):
            db.add(
                RefreshToken(
                    user_id=user.id,
                    family_id=uuid.uuid4(),
                    token_hash=label.ljust(64, "0"),
                    expires_at=now - timedelta(days=age_days),
                )
            )
        await db.commit()

    login = await client.post("/auth/token", data={"username": "pruner", "password": TEST_PASSWORD})
    assert login.status_code == 200

    async with app.state.session_factory() as db:
        hashes = set(await db.scalars(select(RefreshToken.token_hash)))
    assert "ancient".ljust(64, "0") not in hashes, "expired beyond retention -> pruned"
    assert "recent".ljust(64, "0") in hashes, "inside retention -> kept for reuse detection"
    assert len(hashes) == 2, "the recent one + the token just issued"


async def test_refresh_and_login_wait_for_inflight_user_update(
    app: FastAPI, client: AsyncClient, create_user: CreateUser
) -> None:
    """A password change in flight (user row locked, as update_user does) must serialize
    with refresh and login: otherwise a token minted concurrently could escape revoke-all."""
    user = await create_user(username="victim")
    pair = (
        await client.post("/auth/token", data={"username": "victim", "password": TEST_PASSWORD})
    ).json()

    async with app.state.session_factory() as admin_tx:
        await admin_tx.execute(
            select(AppUser).where(AppUser.id == user.id).with_for_update(key_share=True)
        )
        refresh = asyncio.create_task(
            client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
        )
        login = asyncio.create_task(
            client.post("/auth/token", data={"username": "victim", "password": TEST_PASSWORD})
        )
        await asyncio.sleep(0.3)
        assert not refresh.done() and not login.done(), "must wait for the user-row lock"

        # Finish the "password change": new hash + revoke all tokens, then commit.
        await admin_tx.execute(
            update(AppUser)
            .where(AppUser.id == user.id)
            .values(password_hash=await auth_utils.hash_password("a-brand-new-pass"))
        )
        await auth_service.revoke_all_refresh_tokens(admin_tx, user.id)
        await admin_tx.commit()

    assert (await refresh).status_code == 401
    assert (await login).status_code == 401
    async with app.state.session_factory() as db:
        live = await db.scalar(
            select(func.count())
            .select_from(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        )
    assert live == 0, "no refresh token may survive the password change"


async def test_bootstrap_admin_is_not_recreated_after_rename(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth_settings, "FIRST_ADMIN_USERNAME", "admin")
    monkeypatch.setattr(auth_settings, "FIRST_ADMIN_PASSWORD", SecretStr("default-pass"))
    async with app.state.session_factory() as db:
        await auth_service.ensure_first_admin(db)
        await db.execute(update(AppUser).where(AppUser.username == "admin").values(username="boss"))
        await db.commit()

        await auth_service.ensure_first_admin(db)  # e.g. the next restart

        names = set(await db.scalars(select(AppUser.username).where(AppUser.role == Role.ADMIN)))
    assert names == {"boss"}, "a renamed bootstrap admin must not come back with the env password"


async def test_admin_guard_does_not_block_fk_inserts(
    app: FastAPI,
    client: AsyncClient,
    admin: AppUser,
    admin_headers: dict[str, str],
    make_warehouse: MakeWarehouse,
    make_product: MakeProduct,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While the last-admin guard holds the admin rows, those admins can still write
    (operation.performed_by FK takes FOR KEY SHARE, compatible with FOR NO KEY UPDATE)."""
    monkeypatch.setattr(movements_settings, "LOCK_TIMEOUT_MS", 500)
    warehouse, product = await make_warehouse(), await make_product()
    async with app.state.session_factory() as guard_tx:
        assert admin.id in await auth_service.lock_active_admins(guard_tx)
        body = {"type": "IN", "sku": product.sku, "quantity": 1, "to_warehouse_id": warehouse.id}
        r = await client.post("/movements", json=body, headers=admin_headers)
        await guard_tx.rollback()
    assert r.status_code == 201, r.json()
