from fastapi import FastAPI
from httpx import AsyncClient

from tests.conftest import MakeProduct, MakeWarehouse, count_movements


async def test_movement_validation(
    app: FastAPI,
    client: AsyncClient,
    storekeeper_headers: dict[str, str],
    admin_headers: dict[str, str],
    make_warehouse: MakeWarehouse,
    make_product: MakeProduct,
) -> None:
    w1 = await make_warehouse("W1")
    closed = await make_warehouse("CLOSED", is_active=False)
    product = await make_product("P-1")
    gone = await make_product("GONE")
    await client.delete(f"/products/{gone.id}", headers=admin_headers)

    def move(**body: object) -> dict[str, object]:
        return {"sku": "p-1", "quantity": 1, **body}

    rejections = [
        # Shape by type -> 422 before any DB work.
        ("/movements", move(type="IN", from_warehouse_id=w1.id, to_warehouse_id=w1.id), 422),
        ("/movements", move(type="IN"), 422),
        ("/movements", move(type="OUT", to_warehouse_id=w1.id), 422),
        ("/movements", move(type="TRANSFER", from_warehouse_id=w1.id, to_warehouse_id=w1.id), 422),
        ("/movements", move(type="IN", to_warehouse_id=w1.id, quantity=0), 422),
        ("/movements", move(type="LOAN", to_warehouse_id=w1.id), 422),
        # D5: the same SKU twice in a batch (case-insensitive) -> 422; empty batch -> 422.
        (
            "/movements/batch",
            {
                "type": "OUT",
                "from_warehouse_id": w1.id,
                "items": [{"sku": "p-1", "quantity": 5}, {"sku": "P-1", "quantity": 5}],
            },
            422,
        ),
        ("/movements/batch", {"type": "IN", "to_warehouse_id": w1.id, "items": []}, 422),
        # Warehouses: unknown -> 404, inactive -> 409.
        ("/movements", move(type="IN", to_warehouse_id=999_999), 404),
        ("/movements", move(type="IN", to_warehouse_id=closed.id), 409),
        (
            "/movements",
            move(type="TRANSFER", from_warehouse_id=w1.id, to_warehouse_id=closed.id),
            409,
        ),
    ]
    for path, body, expected in rejections:
        r = await client.post(path, json=body, headers=storekeeper_headers)
        assert r.status_code == expected, (body, r.json())

    # Unknown and soft-deleted SKUs: 404 listing ALL of them, in request order.
    missing = await client.post(
        "/movements/batch",
        json={
            "type": "IN",
            "to_warehouse_id": w1.id,
            "items": [
                {"sku": "P-1", "quantity": 1},
                {"sku": "nope", "quantity": 1},
                {"sku": "gone", "quantity": 1},
            ],
        },
        headers=storekeeper_headers,
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "products_not_found"
    assert missing.json()["missing_skus"] == ["NOPE", "GONE"]

    assert (
        await client.post("/movements", json=move(type="IN", to_warehouse_id=w1.id))
    ).status_code == 401
    assert await count_movements(app) == 0, "rejected requests must not write anything"

    # A storekeeper can record IN; it shows up in the product's stock breakdown.
    ok = await client.post(
        "/movements",
        json=move(type="IN", to_warehouse_id=w1.id, quantity=10),
        headers=storekeeper_headers,
    )
    assert ok.status_code == 201
    operation = ok.json()
    assert operation["type"] == "IN"
    assert [(m["sku"], m["quantity"], m["to_warehouse_id"]) for m in operation["movements"]] == [
        ("P-1", 10, w1.id)
    ]
    detail = (await client.get(f"/products/{product.id}", headers=storekeeper_headers)).json()
    assert detail["total_stock"] == 10
    assert detail["stock_by_warehouse"] == [
        {"warehouse_id": w1.id, "location_code": "W1", "quantity": 10}
    ]
