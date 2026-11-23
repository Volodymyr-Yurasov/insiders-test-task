"""Bonus: Idempotency-Key - a repeated request never writes off twice."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from redis.asyncio import Redis
from sqlalchemy import func, select

from src.auth.constants import Role
from src.movements.models import Operation
from src.rate_limit import service as rate_limit_service
from tests.conftest import CreateUser, MakeProduct, MakeWarehouse, auth_headers

REPLAYED = "idempotent-replayed"


async def _operations_with_key(app: FastAPI, key: str) -> int:
    async with app.state.session_factory() as db:
        return (
            await db.scalar(
                select(func.count()).select_from(Operation).where(Operation.idempotency_key == key)
            )
            or 0
        )


async def _stock(client: AsyncClient, h: dict[str, str], product_id: int) -> int:
    detail: dict[str, Any] = (await client.get(f"/products/{product_id}", headers=h)).json()
    return int(detail["total_stock"])


@pytest.fixture
async def stocked(
    client: AsyncClient,
    storekeeper_headers: dict[str, str],
    make_warehouse: MakeWarehouse,
    make_product: MakeProduct,
) -> AsyncIterator[tuple[int, int, str]]:
    warehouse, product = await make_warehouse(), await make_product("IDEM-1")
    body = {"type": "IN", "sku": product.sku, "quantity": 10, "to_warehouse_id": warehouse.id}
    assert (
        await client.post("/movements", json=body, headers=storekeeper_headers)
    ).status_code == 201
    yield warehouse.id, product.id, product.sku


def _out(sku: str, quantity: int, warehouse_id: int) -> dict[str, Any]:
    return {"type": "OUT", "sku": sku, "quantity": quantity, "from_warehouse_id": warehouse_id}


async def test_idempotent_replay(
    app: FastAPI,
    client: AsyncClient,
    storekeeper_headers: dict[str, str],
    create_user: CreateUser,
    stocked: tuple[int, int, str],
) -> None:
    warehouse_id, product_id, sku = stocked
    h = storekeeper_headers

    async def post(
        body: dict[str, Any], key: str, headers: dict[str, str] = h, path: str = "/movements"
    ) -> Response:
        return await client.post(path, json=body, headers={**headers, "Idempotency-Key": key})

    # First call writes; the retry replays the same body without writing again.
    first = await post(_out(sku, 3, warehouse_id), "pay-1")
    assert first.status_code == 201 and REPLAYED not in first.headers
    retry = await post(_out(sku, 3, warehouse_id), "pay-1")
    assert retry.status_code == 201 and retry.headers[REPLAYED] == "true"
    assert retry.json() == first.json()
    assert await _stock(client, h, product_id) == 7
    assert await _operations_with_key(app, "pay-1") == 1

    # Semantically identical body (SKU case) is the same request.
    same = await post(_out(sku.lower(), 3, warehouse_id), "pay-1")
    assert same.status_code == 201 and same.headers[REPLAYED] == "true"

    # Same key, different request (payload, or the batch endpoint) -> 422, nothing written.
    other_qty = await post(_out(sku, 4, warehouse_id), "pay-1")
    assert other_qty.status_code == 422 and other_qty.json()["code"] == "idempotency_key_reused"
    as_batch = {
        "type": "OUT",
        "from_warehouse_id": warehouse_id,
        "items": [{"sku": sku, "quantity": 3}],
    }
    assert (await post(as_batch, "pay-1", path="/movements/batch")).status_code == 422
    assert await _stock(client, h, product_id) == 7

    # Keys are scoped per user: another user's identical key is a different request.
    colleague = auth_headers(await create_user(Role.STOREKEEPER))
    theirs = await post(_out(sku, 3, warehouse_id), "pay-1", headers=colleague)
    assert theirs.status_code == 201 and REPLAYED not in theirs.headers
    assert await _stock(client, h, product_id) == 4

    # A failed request releases its key: the retry runs for real once stock arrives.
    assert (await post(_out(sku, 9, warehouse_id), "pay-2")).status_code == 409
    restock = {"type": "IN", "sku": sku, "quantity": 10, "to_warehouse_id": warehouse_id}
    assert (await client.post("/movements", json=restock, headers=h)).status_code == 201
    real = await post(_out(sku, 9, warehouse_id), "pay-2")
    assert real.status_code == 201 and REPLAYED not in real.headers
    assert await _stock(client, h, product_id) == 5

    # Malformed keys are rejected before anything runs.
    for bad in ("x" * 256, "has space"):
        assert (await post(_out(sku, 1, warehouse_id), bad)).status_code == 422


async def test_concurrent_duplicates_write_once(
    app: FastAPI,
    client: AsyncClient,
    storekeeper_headers: dict[str, str],
    stocked: tuple[int, int, str],
) -> None:
    warehouse_id, product_id, sku = stocked
    headers = {**storekeeper_headers, "Idempotency-Key": "double-click"}
    responses = await asyncio.gather(
        *(
            client.post("/movements", json=_out(sku, 1, warehouse_id), headers=headers)
            for _ in range(5)
        )
    )

    statuses = {r.status_code for r in responses}
    assert statuses <= {201, 409} and 201 in statuses
    for in_flight in (r for r in responses if r.status_code == 409):
        assert in_flight.json()["code"] == "idempotency_in_progress"
    assert len({r.json()["id"] for r in responses if r.status_code == 201}) == 1
    assert await _operations_with_key(app, "double-click") == 1
    assert await _stock(client, storekeeper_headers, product_id) == 9


async def test_db_backstop_when_redis_is_down(
    app: FastAPI,
    client: AsyncClient,
    storekeeper_headers: dict[str, str],
    stocked: tuple[int, int, str],
) -> None:
    """Without Redis, the UNIQUE (performed_by, idempotency_key) claim alone keeps
    exactly-once: duplicates (even concurrent ones) replay from the database."""
    warehouse_id, product_id, sku = stocked
    real, broken = (
        app.state.redis,
        Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2),
    )
    app.state.redis = broken  # by hand: monkeypatch can't restore Starlette's State
    try:
        headers = {**storekeeper_headers, "Idempotency-Key": "no-redis"}
        first = await client.post("/movements", json=_out(sku, 2, warehouse_id), headers=headers)
        retry = await client.post("/movements", json=_out(sku, 2, warehouse_id), headers=headers)
        assert first.status_code == retry.status_code == 201
        assert retry.headers[REPLAYED] == "true" and retry.json() == first.json()
        mismatch = await client.post("/movements", json=_out(sku, 5, warehouse_id), headers=headers)
        assert mismatch.status_code == 422

        racing = {**storekeeper_headers, "Idempotency-Key": "no-redis-race"}
        responses = await asyncio.gather(
            *(
                client.post("/movements", json=_out(sku, 1, warehouse_id), headers=racing)
                for _ in range(3)
            )
        )
        assert [r.status_code for r in responses] == [201, 201, 201]
        assert sum(REPLAYED in r.headers for r in responses) == 2
    finally:
        app.state.redis = real
        await broken.aclose()

    assert await _operations_with_key(app, "no-redis") == 1
    assert await _operations_with_key(app, "no-redis-race") == 1
    assert await _stock(client, storekeeper_headers, product_id) == 7


async def test_corrupt_redis_record_falls_back_to_the_db(
    app: FastAPI,
    client: AsyncClient,
    storekeeper: Any,
    storekeeper_headers: dict[str, str],
    stocked: tuple[int, int, str],
) -> None:
    warehouse_id, product_id, sku = stocked
    await app.state.redis.set(f"idem:{storekeeper.id}:garbled", "not json at all")
    headers = {**storekeeper_headers, "Idempotency-Key": "garbled"}

    first = await client.post("/movements", json=_out(sku, 1, warehouse_id), headers=headers)
    retry = await client.post("/movements", json=_out(sku, 1, warehouse_id), headers=headers)

    assert first.status_code == retry.status_code == 201  # no 500
    assert retry.headers[REPLAYED] == "true"  # the DB constraint still deduplicated
    assert await _stock(client, storekeeper_headers, product_id) == 9


async def test_marker_released_when_failing_before_the_engine(
    app: FastAPI,
    client: AsyncClient,
    storekeeper: Any,
    storekeeper_headers: dict[str, str],
    stocked: tuple[int, int, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected error between claiming the key and running the engine (here: inside
    the rate limiter) must not leave the key stuck "in progress"."""
    warehouse_id, _, sku = stocked

    async def broken_hit(*args: Any) -> Any:
        raise RuntimeError("limiter exploded")

    monkeypatch.setattr(rate_limit_service, "hit", broken_hit)
    headers = {**storekeeper_headers, "Idempotency-Key": "fragile"}
    with pytest.raises(RuntimeError):
        await client.post("/movements", json=_out(sku, 1, warehouse_id), headers=headers)
    assert await app.state.redis.get(f"idem:{storekeeper.id}:fragile") is None
