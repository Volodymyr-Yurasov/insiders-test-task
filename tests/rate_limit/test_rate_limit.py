"""Bonus: sliding-window limit of N movement transactions per minute per employee."""

import asyncio
from collections import Counter
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from redis.asyncio import Redis

from src.auth.constants import Role
from src.rate_limit.config import rate_limit_settings
from tests.conftest import CreateUser, MakeProduct, MakeWarehouse, auth_headers


@pytest.fixture
async def stock_in(
    client: AsyncClient, make_warehouse: MakeWarehouse, make_product: MakeProduct
) -> Any:
    warehouse, product = await make_warehouse(), await make_product()

    async def post(
        headers: dict[str, str], *, key: str | None = None, batch: bool = False
    ) -> Response:
        if key is not None:
            headers = {**headers, "Idempotency-Key": key}
        if batch:
            body: dict[str, Any] = {
                "type": "IN",
                "to_warehouse_id": warehouse.id,
                "items": [{"sku": product.sku, "quantity": 1}],
            }
            return await client.post("/movements/batch", json=body, headers=headers)
        body = {"type": "IN", "sku": product.sku, "quantity": 1, "to_warehouse_id": warehouse.id}
        return await client.post("/movements", json=body, headers=headers)

    return post


async def test_rate_limit_per_user(
    client: AsyncClient,
    storekeeper_headers: dict[str, str],
    create_user: CreateUser,
    stock_in: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rate_limit_settings, "RATE_LIMIT_MAX_REQUESTS", 3)
    h = storekeeper_headers

    first = await stock_in(h, key="k-1")
    assert first.status_code == 201
    assert (first.headers["x-ratelimit-limit"], first.headers["x-ratelimit-remaining"]) == (
        "3",
        "2",
    )
    assert (await stock_in(h, batch=True)).status_code == 201  # a batch is ONE transaction
    third = await stock_in(h)
    assert third.status_code == 201 and third.headers["x-ratelimit-remaining"] == "0"

    limited = await stock_in(h)
    assert limited.status_code == 429
    assert limited.json()["code"] == "rate_limited"
    assert 1 <= int(limited.headers["retry-after"]) <= 60

    # A replay writes nothing: served even while limited, and costs no quota.
    replay = await stock_in(h, key="k-1")
    assert replay.status_code == 201 and replay.headers["idempotent-replayed"] == "true"
    # Reads are never limited; other employees have their own window.
    assert (await client.get("/movements", headers=h)).status_code == 200
    colleague = auth_headers(await create_user(Role.STOREKEEPER))
    assert (await stock_in(colleague)).status_code == 201


async def test_concurrent_requests_respect_the_limit(
    storekeeper_headers: dict[str, str], stock_in: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Lua script is atomic: parallel requests can't all slip under the limit."""
    monkeypatch.setattr(rate_limit_settings, "RATE_LIMIT_MAX_REQUESTS", 5)
    responses = await asyncio.gather(*(stock_in(storekeeper_headers) for _ in range(10)))
    assert Counter(r.status_code for r in responses) == {201: 5, 429: 5}


async def test_window_slides_and_releases_idempotency_key(
    storekeeper_headers: dict[str, str], stock_in: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rate_limit_settings, "RATE_LIMIT_MAX_REQUESTS", 2)
    monkeypatch.setattr(rate_limit_settings, "RATE_LIMIT_WINDOW_S", 1)
    h = storekeeper_headers

    assert [(await stock_in(h)).status_code for _ in range(2)] == [201, 201]
    limited = await stock_in(h, key="after-limit")
    assert limited.status_code == 429

    await asyncio.sleep(1.1)  # the oldest entries slide out of the 1 s window
    retried = await stock_in(h, key="after-limit")
    # Runs for real: the 429 released the key instead of leaving it "in progress".
    assert retried.status_code == 201 and "idempotent-replayed" not in retried.headers


async def test_fail_open_when_redis_is_down(
    app: FastAPI,
    storekeeper_headers: dict[str, str],
    stock_in: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rate_limit_settings, "RATE_LIMIT_MAX_REQUESTS", 1)
    real, broken = (
        app.state.redis,
        Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2),
    )
    app.state.redis = broken  # by hand: monkeypatch can't restore Starlette's State
    try:
        statuses = [(await stock_in(storekeeper_headers)).status_code for _ in range(3)]
    finally:
        app.state.redis = real
        await broken.aclose()
    assert statuses == [201, 201, 201]
