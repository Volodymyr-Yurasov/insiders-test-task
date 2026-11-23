"""Spec 2.4: the valuation is cached in Redis and consistently invalidated."""

import asyncio
from collections import Counter
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient
from redis.asyncio import Redis

from src.reports import cache
from src.reports import service as reports_service
from tests.conftest import MakeProduct, MakeWarehouse, count_statements

VALUATION_SQL_MARK = "LEFT OUTER JOIN stock_movement"


async def _in(client: AsyncClient, h: dict[str, str], sku: str, qty: int, warehouse_id: int) -> int:
    body = {"type": "IN", "sku": sku, "quantity": qty, "to_warehouse_id": warehouse_id}
    return (await client.post("/movements", json=body, headers=h)).status_code


async def test_valuation_cache(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    make_warehouse: MakeWarehouse,
    make_product: MakeProduct,
) -> None:
    h = admin_headers
    warehouse, product = await make_warehouse(), await make_product(price="2.00")
    assert await _in(client, h, product.sku, 5, warehouse.id) == 201

    async def valuation() -> tuple[str, dict[str, Any], int]:
        with count_statements(app) as statements:
            r = await client.get("/reports/valuation", headers=h)
        assert r.status_code == 200
        valuation_queries = sum(VALUATION_SQL_MARK in s for s in statements)
        return r.headers["x-cache"], r.json(), valuation_queries

    # MISS computes; HIT is served from Redis without touching the valuation SQL.
    status, report, queries = await valuation()
    assert (status, report["grand_total"], queries) == ("MISS", "10.00", 1)
    status, cached, queries = await valuation()
    assert (status, queries) == ("HIT", 0) and cached == report

    # A new movement invalidates: next read recomputes with the new numbers.
    assert await _in(client, h, product.sku, 5, warehouse.id) == 201
    status, report, _ = await valuation()
    assert (status, report["grand_total"]) == ("MISS", "20.00")

    # Every successful write to a table the report reads bumps the generation; a failed one
    # (rolled back) does not.
    redis: Redis = app.state.redis
    spare = await make_product()
    writes: list[tuple[str, str, dict[str, Any] | None]] = [
        (
            "POST",
            "/movements",
            {"type": "IN", "sku": product.sku, "quantity": 1, "to_warehouse_id": warehouse.id},
        ),
        (
            "POST",
            "/movements/batch",
            {
                "type": "IN",
                "to_warehouse_id": warehouse.id,
                "items": [{"sku": product.sku, "quantity": 1}],
            },
        ),
        ("POST", "/products", {"sku": "NEW-1", "name": "New", "base_price": "1.00"}),
        ("PATCH", f"/products/{product.id}", {"base_price": "3.00"}),
        ("DELETE", f"/products/{spare.id}", None),
        ("POST", "/warehouses", {"name": "New", "location_code": "NEW-W", "address": "x"}),
        ("PATCH", f"/warehouses/{warehouse.id}", {"name": "Renamed"}),
    ]
    for method, path, body in writes:
        before = await cache.current_generation(redis)
        r = await client.request(method, path, json=body, headers=h)
        assert r.status_code in (200, 201, 204), (path, r.json())
        assert await cache.current_generation(redis) == before + 1, (
            f"{method} {path} must invalidate"
        )
    before = await cache.current_generation(redis)
    oversell = {
        "type": "OUT",
        "sku": product.sku,
        "quantity": 10_000,
        "from_warehouse_id": warehouse.id,
    }
    assert (await client.post("/movements", json=oversell, headers=h)).status_code == 409
    assert await cache.current_generation(redis) == before, "rolled-back writes don't invalidate"

    # Redis down: the report is still served from the DB, and writes still succeed.
    # (Swapped by hand: monkeypatch can't restore attributes on Starlette's State object.)
    real, broken = redis, Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    app.state.redis = broken
    try:
        status, report, queries = await valuation()
        assert (status, queries) == ("BYPASS", 1)
        assert await _in(client, h, product.sku, 1, warehouse.id) == 201
    finally:
        app.state.redis = real
        await broken.aclose()


async def test_stale_write_back_is_never_served(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    make_warehouse: MakeWarehouse,
    make_product: MakeProduct,
) -> None:
    """Deterministic replay of the classic cache race the generation counter exists for."""
    h, redis = admin_headers, app.state.redis
    warehouse, product = await make_warehouse(), await make_product(price="1.00")
    assert await _in(client, h, product.sku, 1, warehouse.id) == 201

    # 1. A slow reader reads the generation and computes from the DB (value 1.00)...
    generation = await cache.current_generation(redis)
    async with app.state.session_factory() as db:
        stale = await reports_service.compute_valuation(db)
    # 2. ...meanwhile a movement commits and invalidates (value now 6.00)...
    assert await _in(client, h, product.sku, 5, warehouse.id) == 201
    # 3. ...then the slow reader finally stores its stale result.
    await cache.store(redis, generation, stale)

    r = await client.get("/reports/valuation", headers=h)
    assert r.headers["x-cache"] == "MISS"
    assert r.json()["grand_total"] == "6.00", "the stale write-back must never be served"


async def test_cold_cache_stampede_computes_once(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    make_warehouse: MakeWarehouse,
    make_product: MakeProduct,
) -> None:
    warehouse, product = await make_warehouse(), await make_product()
    assert await _in(client, admin_headers, product.sku, 3, warehouse.id) == 201

    with count_statements(app) as statements:
        responses = await asyncio.gather(
            *(client.get("/reports/valuation", headers=admin_headers) for _ in range(10))
        )

    assert all(r.status_code == 200 for r in responses)
    assert Counter(r.headers["x-cache"] for r in responses) == {"MISS": 1, "HIT": 9}
    assert sum(VALUATION_SQL_MARK in s for s in statements) == 1
    assert len({r.json()["generated_at"] for r in responses}) == 1


async def test_redis_failure_after_compute_does_not_recompute() -> None:
    """Redis dies between computing and storing: answer once, never run the query twice."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    from src.reports.schemas import ValuationReport

    class FlakyRedis:
        async def get(self, key: str) -> None:
            return None

        async def set(self, key: str, value: str, **kwargs: Any) -> bool:
            if kwargs.get("nx"):
                return True  # we win the recompute lock
            raise RedisConnectionError("gone while storing")

        async def eval(self, *args: Any) -> int:
            raise RedisConnectionError("gone while releasing")

    calls = 0

    async def compute() -> ValuationReport:
        nonlocal calls
        calls += 1
        return ValuationReport(
            warehouses=[], grand_total="0.00", generated_at="2026-01-01T00:00:00Z"
        )

    report, status = await cache.get_or_compute(FlakyRedis(), compute)  # type: ignore[arg-type]
    assert (calls, status, str(report.grand_total)) == (1, "MISS", "0.00")
