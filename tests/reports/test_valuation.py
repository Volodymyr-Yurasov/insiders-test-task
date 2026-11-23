from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import insert

from src.movements.models import Operation, StockMovement
from tests.conftest import MakeProduct, MakeWarehouse


async def test_valuation(
    client: AsyncClient,
    admin_headers: dict[str, str],
    storekeeper_headers: dict[str, str],
    make_warehouse: MakeWarehouse,
    make_product: MakeProduct,
) -> None:
    h = admin_headers
    w1, w2 = await make_warehouse("W1"), await make_warehouse("W2")
    empty, closed = await make_warehouse("EMPTY"), await make_warehouse("CLOSED", is_active=False)
    p1 = await make_product("P1", price="2.50")
    await make_product("P2", price="10.00")

    for body in (
        {"type": "IN", "sku": "P1", "quantity": 10, "to_warehouse_id": w1.id},
        {"type": "IN", "sku": "P2", "quantity": 3, "to_warehouse_id": w1.id},
        {
            "type": "TRANSFER",
            "sku": "P1",
            "quantity": 4,
            "from_warehouse_id": w1.id,
            "to_warehouse_id": w2.id,
        },
        {"type": "OUT", "sku": "P2", "quantity": 1, "from_warehouse_id": w1.id},
    ):
        assert (await client.post("/movements", json=body, headers=h)).status_code == 201

    async def valuation() -> dict[str, Any]:
        r = await client.get("/reports/valuation", headers=h)
        assert r.status_code == 200
        report: dict[str, Any] = r.json()
        return report

    report = await valuation()
    # W1: P1 6 * 2.50 + P2 2 * 10.00 = 35.00 ; W2: P1 4 * 2.50 = 10.00
    assert [
        (w["warehouse_id"], w["is_active"], w["total_units"], w["total_value"])
        for w in report["warehouses"]
    ] == [
        (w1.id, True, 8, "35.00"),
        (w2.id, True, 4, "10.00"),
        (empty.id, True, 0, "0.00"),
        (closed.id, False, 0, "0.00"),
    ]
    assert report["grand_total"] == "45.00"
    assert "generated_at" in report

    # Valuation uses the *current* base price.
    await client.patch(f"/products/{p1.id}", json={"base_price": "3.00"}, headers=h)
    repriced = await valuation()
    assert [w["total_value"] for w in repriced["warehouses"][:2]] == ["38.00", "12.00"]
    assert repriced["grand_total"] == "50.00"

    # Admin only.
    assert (await client.get("/reports/valuation", headers=storekeeper_headers)).status_code == 403
    assert (await client.get("/reports/valuation")).status_code == 401


async def test_valuation_handles_huge_values(
    app: FastAPI,
    client: AsyncClient,
    admin: Any,
    admin_headers: dict[str, str],
    make_warehouse: MakeWarehouse,
    make_product: MakeProduct,
) -> None:
    """Max price x many max-quantity INs exceeds numeric(20,2): must still be exact, not 500."""
    warehouse = await make_warehouse()
    product = await make_product(price="9999999999.99")
    async with app.state.session_factory() as db:  # direct inserts: past API rate limits
        operation_id = await db.scalar(
            insert(Operation).values(type="IN", performed_by=admin.id).returning(Operation.id)
        )
        await db.execute(
            insert(StockMovement),
            [
                {
                    "operation_id": operation_id,
                    "type": "IN",
                    "product_id": product.id,
                    "quantity": 1_000_000,
                    "to_warehouse_id": warehouse.id,
                    "performed_by": admin.id,
                }
            ]
            * 200,
        )
        await db.commit()

    r = await client.get("/reports/valuation", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["grand_total"] == "1999999999998000000.00"  # 2e8 units * 9999999999.99
