"""Spec 2.3: aggregations run a constant number of SQL statements, whatever the data size."""

from fastapi import FastAPI
from httpx import AsyncClient

from tests.conftest import MakeProduct, MakeWarehouse, count_statements


async def test_aggregations_use_constant_queries(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    make_warehouse: MakeWarehouse,
    make_product: MakeProduct,
) -> None:
    w1, w2 = await make_warehouse(), await make_warehouse()
    paths = [
        "/products?sort=stock&order=desc",
        f"/products?warehouse_id={w1.id}&below_min_stock=true",
        "/reports/valuation",
    ]

    async def add_stocked_products(count: int) -> None:
        for _ in range(count):
            product = await make_product()
            for warehouse in (w1, w2):
                body = {
                    "type": "IN",
                    "sku": product.sku,
                    "quantity": 3,
                    "to_warehouse_id": warehouse.id,
                }
                assert (
                    await client.post("/movements", json=body, headers=admin_headers)
                ).status_code == 201

    async def statements_per_request() -> dict[str, int]:
        counts = {}
        for path in paths:
            with count_statements(app) as statements:
                response = await client.get(path, headers=admin_headers)
            assert response.status_code == 200, response.json()
            counts[path] = len(statements)
        return counts

    await add_stocked_products(3)
    small = await statements_per_request()
    await add_stocked_products(9)
    large = await statements_per_request()

    # 1 = the auth dependency's user lookup; the rest is the endpoint's own data access.
    assert small == {
        paths[0]: 1 + 1,  # one list statement
        paths[1]: 1 + 2,  # warehouse_id existence check + one list statement
        paths[2]: 1 + 1,  # one valuation statement
    }
    assert large == small, "statement count grew with the data: N+1"
