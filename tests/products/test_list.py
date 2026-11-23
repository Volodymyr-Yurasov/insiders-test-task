from httpx import AsyncClient

from tests.conftest import MakeProduct, MakeWarehouse


async def test_product_list(
    client: AsyncClient,
    admin_headers: dict[str, str],
    make_warehouse: MakeWarehouse,
    make_product: MakeProduct,
) -> None:
    h = admin_headers
    w1, w2 = await make_warehouse("W1"), await make_warehouse("W2")
    await make_product("BOLT_M8", price="1.00", min_stock_level=10)  # total 25 (W1 20, W2 5)
    await make_product("BOLTXM8", price="2.50")  # total 3 (W1)
    await make_product("NUT-1", price="0.50", min_stock_level=5)  # no movements -> 0
    gone = await make_product("GONE", price="9.00")
    await make_product("WASHER", price="5.00", min_stock_level=1)  # total 8 (W2)
    assert (await client.delete(f"/products/{gone.id}", headers=h)).status_code == 204

    for sku, qty, warehouse in (
        ("BOLT_M8", 20, w1),
        ("BOLT_M8", 5, w2),
        ("BOLTXM8", 3, w1),
        ("WASHER", 8, w2),
    ):
        body = {"type": "IN", "sku": sku, "quantity": qty, "to_warehouse_id": warehouse.id}
        assert (await client.post("/movements", json=body, headers=h)).status_code == 201

    async def skus(query: str = "") -> list[str]:
        r = await client.get(f"/products?{query}", headers=h)
        assert r.status_code == 200, r.json()
        return [item["sku"] for item in r.json()["items"]]

    # Default: id order, deleted products never listed, stock computed (0 without movements).
    listing = (await client.get("/products", headers=h)).json()
    assert [(i["sku"], i["stock"]) for i in listing["items"]] == [
        ("BOLT_M8", 25),
        ("BOLTXM8", 3),
        ("NUT-1", 0),
        ("WASHER", 8),
    ]
    assert listing["total"] == 4 and listing["items"][0]["base_price"] == "1.00"

    # Sorting by stock and by price; pagination over a sort.
    assert await skus("sort=stock&order=desc") == ["BOLT_M8", "WASHER", "BOLTXM8", "NUT-1"]
    assert await skus("sort=price") == ["NUT-1", "BOLT_M8", "BOLTXM8", "WASHER"]
    page2 = (await client.get("/products?sort=stock&order=desc&limit=2&offset=2", headers=h)).json()
    assert [i["sku"] for i in page2["items"]] == ["BOLTXM8", "NUT-1"] and page2["total"] == 4

    # Filters.
    assert await skus("min_stock=5") == ["BOLT_M8", "WASHER"]
    assert await skus("max_price=1") == ["BOLT_M8", "NUT-1"]
    assert await skus("below_min_stock=true") == ["NUT-1"]
    assert await skus("search=bolt") == ["BOLT_M8", "BOLTXM8"]
    assert await skus("search=_") == ["BOLT_M8"], "'_' must match literally, not any char"

    # Per-warehouse stock: W2 holds BOLT_M8=5, WASHER=8; below_min compares that stock.
    in_w2 = (await client.get(f"/products?warehouse_id={w2.id}", headers=h)).json()
    assert [(i["sku"], i["stock"]) for i in in_w2["items"]] == [
        ("BOLT_M8", 5),
        ("BOLTXM8", 0),
        ("NUT-1", 0),
        ("WASHER", 8),
    ]
    assert await skus(f"warehouse_id={w2.id}&below_min_stock=true") == ["BOLT_M8", "NUT-1"]

    # Invalid input never reaches SQL.
    for bad in ("sort=base_price;drop", "order=sideways", "min_stock=5&max_stock=1", "limit=0"):
        assert (await client.get(f"/products?{bad}", headers=h)).status_code == 422, bad
    unknown = await client.get("/products?warehouse_id=999999", headers=h)
    assert unknown.status_code == 404 and unknown.json()["code"] == "warehouse_not_found"
