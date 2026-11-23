import asyncio
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import func, select

from src.auth import service as auth_service
from src.auth import utils as auth_utils
from src.auth.config import auth_settings
from src.auth.constants import Role
from src.auth.models import AppUser
from tests.conftest import TEST_PASSWORD, CreateUser, auth_headers


async def test_login(app: FastAPI, client: AsyncClient) -> None:
    async with app.state.session_factory() as db:
        user = await auth_service.create_user(
            db, username="Keeper", password="correct-horse", role=Role.STOREKEEPER
        )
        await auth_service.create_user(
            db, username="retired", password="correct-horse", role=Role.STOREKEEPER, is_active=False
        )
    assert user is not None

    # Username is normalized (case + whitespace) on both create and login.
    ok = await client.post(
        "/auth/token", data={"username": " KEEPER ", "password": "correct-horse"}
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["token_type"] == "bearer"
    claims = jwt.decode(
        body["access_token"],
        auth_settings.JWT_SECRET.get_secret_value(),
        algorithms=[auth_settings.JWT_ALG],
    )
    assert claims["sub"] == str(user.id)

    # Wrong password, unknown user, inactive user: indistinguishable responses.
    failures = [
        await client.post("/auth/token", data={"username": "keeper", "password": "wrong-pass"}),
        await client.post("/auth/token", data={"username": "ghost", "password": "correct-horse"}),
        await client.post("/auth/token", data={"username": "retired", "password": "correct-horse"}),
    ]
    for r in failures:
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"
        assert r.json() == {
            "code": "invalid_credentials",
            "detail": "Incorrect username or password",
        }


def _forged(user_id: int, *, secret: str, expires_in: timedelta) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + expires_in}, secret, algorithm="HS256"
    )


@pytest.mark.parametrize(
    ("case", "method", "path", "expected_status", "expected_code"),
    [
        ("no_token", "GET", "/auth/me", 401, "not_authenticated"),
        ("garbage", "GET", "/auth/me", 401, "invalid_token"),
        ("expired", "GET", "/auth/me", 401, "invalid_token"),
        ("wrong_signature", "GET", "/auth/me", 401, "invalid_token"),
        ("unknown_user", "GET", "/auth/me", 401, "invalid_token"),
        ("deactivated", "GET", "/auth/me", 401, "invalid_token"),
        ("storekeeper", "GET", "/auth/me", 200, None),
        ("storekeeper", "POST", "/users", 403, "permission_denied"),
        ("storekeeper", "GET", "/users/1", 403, "permission_denied"),
    ],
)
async def test_access_matrix(
    client: AsyncClient,
    create_user: CreateUser,
    storekeeper: AppUser,
    case: str,
    method: str,
    path: str,
    expected_status: int,
    expected_code: str | None,
) -> None:
    secret = auth_settings.JWT_SECRET.get_secret_value()
    match case:
        case "no_token":
            headers = {}
        case "garbage":
            headers = {"Authorization": "Bearer not-a-jwt"}
        case "expired":
            token = auth_utils.create_access_token(storekeeper.id, expires_delta=timedelta(-1))
            headers = {"Authorization": f"Bearer {token}"}
        case "wrong_signature":
            forged = _forged(storekeeper.id, secret="x" * 40, expires_in=timedelta(minutes=5))
            headers = {"Authorization": f"Bearer {forged}"}
        case "unknown_user":
            token = _forged(999_999, secret=secret, expires_in=timedelta(minutes=5))
            headers = {"Authorization": f"Bearer {token}"}
        case "deactivated":
            headers = auth_headers(await create_user(is_active=False))
        case _:
            headers = auth_headers(storekeeper)

    body = {"username": "newbie", "password": "password123", "role": "storekeeper"}
    response = await client.request(
        method, path, headers=headers, json=body if method == "POST" else None
    )

    assert response.status_code == expected_status
    if expected_code is not None:
        assert response.json()["code"] == expected_code
    else:
        assert response.json()["id"] == storekeeper.id


async def test_admin_manages_users(client: AsyncClient, admin_headers: dict[str, str]) -> None:
    created = await client.post(
        "/users",
        headers=admin_headers,
        json={"username": " New.Keeper ", "password": "password123", "role": "storekeeper"},
    )
    assert created.status_code == 201
    user = created.json()
    assert user["username"] == "new.keeper"
    assert user["role"] == "storekeeper"
    assert "password_hash" not in user and "password" not in user

    duplicate = await client.post(
        "/users",
        headers=admin_headers,
        json={"username": "NEW.KEEPER", "password": "password123", "role": "admin"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "username_taken"

    invalid = await client.post(
        "/users",
        headers=admin_headers,
        json={"username": "x", "password": "short", "role": "superuser"},
    )
    assert invalid.status_code == 422
    assert {e["loc"][-1] for e in invalid.json()["errors"]} == {"username", "password", "role"}

    fetched = await client.get(f"/users/{user['id']}", headers=admin_headers)
    assert fetched.status_code == 200 and fetched.json() == user
    missing = await client.get("/users/999999", headers=admin_headers)
    assert missing.status_code == 404 and missing.json()["code"] == "user_not_found"

    # Pagination: the admin fixture + "new.keeper" + 2 more = 4 users, ordered by id.
    for name in ("third", "fourth"):
        await client.post(
            "/users",
            headers=admin_headers,
            json={"username": name, "password": "password123", "role": "storekeeper"},
        )
    first_page = (await client.get("/users?limit=2", headers=admin_headers)).json()
    assert first_page["total"] == 4 and first_page["limit"] == 2 and first_page["offset"] == 0
    assert [u["id"] for u in first_page["items"]] == sorted(u["id"] for u in first_page["items"])
    assert len(first_page["items"]) == 2
    past_end = (await client.get("/users?offset=50", headers=admin_headers)).json()
    assert past_end["items"] == [] and past_end["total"] == 4
    assert (await client.get("/users?limit=0", headers=admin_headers)).status_code == 422


async def test_refresh_rotation(client: AsyncClient, create_user: CreateUser) -> None:
    user = await create_user(username="rotator")

    async def login() -> dict[str, str]:
        r = await client.post(
            "/auth/token", data={"username": "rotator", "password": TEST_PASSWORD}
        )
        assert r.status_code == 200
        pair: dict[str, str] = r.json()
        return pair

    async def refresh(token: str) -> Response:
        return await client.post("/auth/refresh", json={"refresh_token": token})

    # 1. Rotation: a new pair whose access token works.
    first = await login()
    rotated = await refresh(first["refresh_token"])
    assert rotated.status_code == 200
    second = rotated.json()
    assert second["refresh_token"] != first["refresh_token"]
    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {second['access_token']}"})
    assert me.status_code == 200 and me.json()["id"] == user.id

    # 2. Reuse of a rotated token = theft signal: 401, and the whole chain dies.
    reused = await refresh(first["refresh_token"])
    assert reused.status_code == 401 and reused.json()["code"] == "invalid_refresh_token"
    assert (await refresh(second["refresh_token"])).status_code == 401

    # 3. Concurrent refreshes with the same token: exactly one wins.
    third = await login()
    results = await asyncio.gather(*(refresh(third["refresh_token"]) for _ in range(2)))
    assert sorted(r.status_code for r in results) == [200, 401]

    # 4. Logout revokes; it is idempotent and silent for unknown tokens.
    fourth = await login()
    logout = await client.post("/auth/logout", json={"refresh_token": fourth["refresh_token"]})
    assert logout.status_code == 204
    assert (await refresh(fourth["refresh_token"])).status_code == 401
    for token in (fourth["refresh_token"], "never-issued"):
        assert (await client.post("/auth/logout", json={"refresh_token": token})).status_code == 204


async def test_update_user(
    app: FastAPI, client: AsyncClient, create_user: CreateUser, admin: AppUser
) -> None:
    admin_h = auth_headers(admin)

    async def patch(user_id: int, body: dict[str, object], headers: dict[str, str]) -> Response:
        return await client.patch(f"/users/{user_id}", json=body, headers=headers)

    # 1. The sole admin can't demote or deactivate themselves.
    demotions: list[dict[str, object]] = [{"role": "storekeeper"}, {"is_active": False}]
    for body in demotions:
        r = await patch(admin.id, body, admin_h)
        assert r.status_code == 409 and r.json()["code"] == "last_admin"

    # 2. Edit: rename + demote another admin; updated_at moves; validation and conflicts.
    target = await create_user(Role.ADMIN, username="target")
    r = await patch(target.id, {"username": " Renamed ", "role": "storekeeper"}, admin_h)
    assert r.status_code == 200
    edited = r.json()
    assert edited["username"] == "renamed" and edited["role"] == "storekeeper"
    assert datetime.fromisoformat(edited["updated_at"]) > datetime.fromisoformat(
        edited["created_at"]
    )
    assert (await patch(target.id, {}, admin_h)).status_code == 422
    taken = await patch(target.id, {"username": admin.username}, admin_h)
    assert taken.status_code == 409 and taken.json()["code"] == "username_taken"
    assert (await patch(999_999, {"is_active": False}, admin_h)).status_code == 404

    # 3. Password change and deactivation revoke refresh tokens; deactivation kills access.
    await create_user(username="keeper")

    async def login(password: str) -> Response:
        return await client.post("/auth/token", data={"username": "keeper", "password": password})

    old_pair = (await login(TEST_PASSWORD)).json()
    keeper_id = (await client.get("/auth/me", headers=_bearer(old_pair))).json()["id"]
    assert (await patch(keeper_id, {"password": "brand-new-pass"}, admin_h)).status_code == 200
    assert (await _refresh(client, old_pair)).status_code == 401
    assert (await login(TEST_PASSWORD)).status_code == 401
    new_pair = (await login("brand-new-pass")).json()

    assert (await patch(keeper_id, {"is_active": False}, admin_h)).status_code == 200
    assert (await client.get("/auth/me", headers=_bearer(new_pair))).status_code == 401
    assert (await _refresh(client, new_pair)).status_code == 401

    # 4. Two admins demote each other concurrently: the system keeps an active admin.
    other = await create_user(Role.ADMIN, username="other-admin")
    results = await asyncio.gather(
        patch(other.id, {"role": "storekeeper"}, admin_h),
        patch(admin.id, {"role": "storekeeper"}, auth_headers(other)),
    )
    statuses = sorted(r.status_code for r in results)
    # The loser is rejected by the last-admin guard (409) or, if it was already demoted
    # before its auth check ran, by the role check (403). Never both succeed.
    assert statuses[0] == 200 and statuses[1] in (403, 409), statuses
    async with app.state.session_factory() as db:
        active_admins = await db.scalar(
            select(func.count())
            .select_from(AppUser)
            .where(AppUser.role == Role.ADMIN, AppUser.is_active)
        )
    assert active_admins == 1


def _bearer(pair: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {pair['access_token']}"}


async def _refresh(client: AsyncClient, pair: dict[str, str]) -> Response:
    return await client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
