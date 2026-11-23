"""The three required integration tests (spec section 3) plus the lock-timeout path."""

import asyncio
from collections import Counter
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import select

from src.movements.config import movements_settings
from src.products.models import Product
from tests.conftest import MakeProduct, MakeWarehouse, count_movements


def _movement(
    type_: str, sku: str, quantity: int, src: int | None = None, dst: int | None = None
) -> dict[str, Any]:
    return {
        "type": type_,
        "sku": sku,
        "quantity": quantity,
        "from_warehouse_id": src,
        "to_warehouse_id": dst,
    }


async def _post(client: AsyncClient, headers: dict[str, str], body: dict[str, Any]) -> Response:
    path = "/movements/batch" if "items" in body else "/movements"
    return await client.post(path, json=body, headers=headers)


async def _stock(client: AsyncClient, headers: dict[str, str], product_id: int) -> dict[str, Any]:
    detail: dict[str, Any] = (await client.get(f"/products/{product_id}", headers=headers)).json()
    return {
        "total": detail["total_stock"],
        "by_warehouse": {s["warehouse_id"]: s["quantity"] for s in detail["stock_by_warehouse"]},
    }


async def test_concurrent_out_never_oversells(
    client: AsyncClient,
    storekeeper_headers: dict[str, str],
    make_warehouse: MakeWarehouse,
    make_product: MakeProduct,
) -> None:
    """Required: 10 parallel write-offs of 1 against a stock of 5 (asyncio.gather)."""
    warehouse, product = await make_warehouse(), await make_product()
    seeded = await _post(
        client, storekeeper_headers, _movement("IN", product.sku, 5, dst=warehouse.id)
    )
    assert seeded.status_code == 201

    out = _movement("OUT", product.sku, 1, src=warehouse.id)
    results = await asyncio.gather(*(_post(client, storekeeper_headers, out) for _ in range(10)))

    assert Counter(r.status_code for r in results) == {201: 5, 409: 5}
    for rejected in (r for r in results if r.status_code == 409):
        assert rejected.json()["shortages"] == [
            {"sku": product.sku, "requested": 1, "available": 0, "missing": 1}
        ]
    assert await _stock(client, storekeeper_headers, product.id) == {"total": 0, "by_warehouse": {}}


async def test_batch_is_all_or_nothing(
    app: FastAPI,
    client: AsyncClient,
    storekeeper_headers: dict[str, str],
    make_warehouse: MakeWarehouse,
    make_product: MakeProduct,
) -> None:
    """Required: one short line rolls back the whole batch; the 409 names every shortage."""
    warehouse = await make_warehouse()
    a, b, c = await make_product("A"), await make_product("B"), await make_product("C")
    for sku, qty in (("A", 10), ("B", 2)):
        await _post(client, storekeeper_headers, _movement("IN", sku, qty, dst=warehouse.id))
    movements_before = await count_movements(app)

    batch = {
        "type": "OUT",
        "from_warehouse_id": warehouse.id,
        "items": [
            {"sku": "A", "quantity": 5},
            {"sku": "B", "quantity": 3},
            {"sku": "C", "quantity": 1},
        ],
    }
    r = await _post(client, storekeeper_headers, batch)

    assert r.status_code == 409
    assert r.json()["code"] == "insufficient_stock"
    assert r.json()["shortages"] == [
        {"sku": "B", "requested": 3, "available": 2, "missing": 1},
        {"sku": "C", "requested": 1, "available": 0, "missing": 1},
    ]
    assert await count_movements(app) == movements_before, "nothing may be written"
    for product, expected in ((a, 10), (b, 2), (c, 0)):
        assert (await _stock(client, storekeeper_headers, product.id))["total"] == expected


async def test_stock_after_in_transfer_out(
    client: AsyncClient,
    storekeeper_headers: dict[str, str],
    make_warehouse: MakeWarehouse,
    make_product: MakeProduct,
) -> None:
    """Required: dynamic stock is correct after IN -> TRANSFER -> OUT."""
    w1, w2 = await make_warehouse("W1"), await make_warehouse("W2")
    product = await make_product()
    h = storekeeper_headers

    assert (await _post(client, h, _movement("IN", product.sku, 10, dst=w1.id))).status_code == 201
    assert (
        await _post(client, h, _movement("TRANSFER", product.sku, 4, src=w1.id, dst=w2.id))
    ).status_code == 201
    assert (await _post(client, h, _movement("OUT", product.sku, 3, src=w2.id))).status_code == 201

    assert await _stock(client, h, product.id) == {"total": 7, "by_warehouse": {w1.id: 6, w2.id: 1}}
    # And W2 can't give more than it has left.
    over = await _post(client, h, _movement("OUT", product.sku, 2, src=w2.id))
    assert over.status_code == 409 and over.json()["shortages"][0]["available"] == 1

    # What each warehouse currently holds.
    for warehouse_id, expected in ((w1.id, 6), (w2.id, 1)):
        held = (await client.get(f"/warehouses/{warehouse_id}/stock", headers=h)).json()
        assert [(i["sku"], i["quantity"]) for i in held["items"]] == [(product.sku, expected)]

    # History: newest first; warehouse filter matches either side.
    history = (await client.get(f"/movements?product_id={product.id}", headers=h)).json()
    assert [(m["type"], m["quantity"]) for m in history["items"]] == [
        ("OUT", 3),
        ("TRANSFER", 4),
        ("IN", 10),
    ]
    at_w2 = (await client.get(f"/movements?warehouse_id={w2.id}", headers=h)).json()
    assert [m["type"] for m in at_w2["items"]] == ["OUT", "TRANSFER"] and at_w2["total"] == 2


async def test_lock_timeout_returns_503(
    app: FastAPI,
    client: AsyncClient,
    storekeeper_headers: dict[str, str],
    make_warehouse: MakeWarehouse,
    make_product: MakeProduct,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warehouse, product = await make_warehouse(), await make_product()
    monkeypatch.setattr(movements_settings, "LOCK_TIMEOUT_MS", 200)

    async with app.state.session_factory() as blocker:
        # Another transaction holds the product row lock and doesn't let go.
        await blocker.execute(select(Product).where(Product.id == product.id).with_for_update())
        r = await _post(
            client, storekeeper_headers, _movement("OUT", product.sku, 1, src=warehouse.id)
        )
        await blocker.rollback()

    assert r.status_code == 503
    assert r.json()["code"] == "service_busy"
    assert r.headers["retry-after"] == "1"
