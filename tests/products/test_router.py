from httpx import AsyncClient


async def test_product_crud(
    client: AsyncClient, admin_headers: dict[str, str], storekeeper_headers: dict[str, str]
) -> None:
    body = {"sku": " ab-100 ", "name": " Bolt M8 ", "base_price": "12.5", "min_stock_level": 10}

    # Create (admin): SKU normalized, price stored exactly and returned as a string.
    created = await client.post("/products", json=body, headers=admin_headers)
    assert created.status_code == 201
    product = created.json()
    assert (product["sku"], product["name"], product["base_price"]) == (
        "AB-100",
        "Bolt M8",
        "12.50",
    )
    assert "is_deleted" not in product
    denied = await client.post("/products", json=body, headers=storekeeper_headers)
    assert denied.status_code == 403

    # Invalid money / SKU -> 422.
    for bad in ({"base_price": "-1"}, {"base_price": "10.005"}, {"sku": "bad sku"}):
        r = await client.post(
            "/products", json={**body, "sku": "OK-1", **bad}, headers=admin_headers
        )
        assert r.status_code == 422, bad

    url = f"/products/{product['id']}"
    # Read: any authenticated user.
    detail = (await client.get(url, headers=storekeeper_headers)).json()
    assert detail == {**product, "total_stock": 0, "stock_by_warehouse": []}

    # Patch (admin): price + threshold; SKU is immutable (extra field rejected).
    patched = await client.patch(
        url, json={"base_price": 15, "min_stock_level": 3}, headers=admin_headers
    )
    assert patched.status_code == 200
    assert (patched.json()["base_price"], patched.json()["min_stock_level"]) == ("15.00", 3)
    assert (await client.patch(url, json={"sku": "NEW"}, headers=admin_headers)).status_code == 422
    assert (
        await client.patch(url, json={"name": "x"}, headers=storekeeper_headers)
    ).status_code == 403

    # Soft delete: storekeeper can't; then the product is gone everywhere...
    assert (await client.delete(url, headers=storekeeper_headers)).status_code == 403
    assert (await client.delete(url, headers=admin_headers)).status_code == 204
    for method, kwargs in (("GET", {}), ("PATCH", {"json": {"name": "x"}}), ("DELETE", {})):
        r = await client.request(method, url, headers=admin_headers, **kwargs)  # type: ignore[arg-type]
        assert r.status_code == 404 and r.json()["code"] == "product_not_found", method

    # ...but its SKU stays reserved (D9).
    reuse = await client.post("/products", json=body, headers=admin_headers)
    assert reuse.status_code == 409 and reuse.json()["code"] == "sku_taken"
