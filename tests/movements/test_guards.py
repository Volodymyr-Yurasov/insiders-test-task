from httpx import AsyncClient

from tests.conftest import MakeProduct, MakeWarehouse


async def test_stock_guards(
    client: AsyncClient,
    admin_headers: dict[str, str],
    make_warehouse: MakeWarehouse,
    make_product: MakeProduct,
) -> None:
    """D8: no deactivating a warehouse or deleting a product while stock remains."""
    warehouse, product = await make_warehouse(), await make_product()
    h = admin_headers

    async def move(type_: str, **route: int) -> None:
        body = {"type": type_, "sku": product.sku, "quantity": 5, **route}
        assert (await client.post("/movements", json=body, headers=h)).status_code == 201

    await move("IN", to_warehouse_id=warehouse.id)

    deactivate = await client.patch(
        f"/warehouses/{warehouse.id}", json={"is_active": False}, headers=h
    )
    assert deactivate.status_code == 409 and deactivate.json()["code"] == "warehouse_not_empty"
    delete = await client.delete(f"/products/{product.id}", headers=h)
    assert delete.status_code == 409 and delete.json()["code"] == "product_has_stock"
    # Other edits of a stocked warehouse are fine.
    rename = await client.patch(f"/warehouses/{warehouse.id}", json={"name": "Renamed"}, headers=h)
    assert rename.status_code == 200

    # Once the stock is written off, both are allowed.
    await move("OUT", from_warehouse_id=warehouse.id)
    deactivate = await client.patch(
        f"/warehouses/{warehouse.id}", json={"is_active": False}, headers=h
    )
    assert deactivate.status_code == 200 and deactivate.json()["is_active"] is False
    assert (await client.delete(f"/products/{product.id}", headers=h)).status_code == 204
