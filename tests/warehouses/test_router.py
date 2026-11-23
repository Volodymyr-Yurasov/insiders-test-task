from httpx import AsyncClient


async def test_warehouse_crud(
    client: AsyncClient, admin_headers: dict[str, str], storekeeper_headers: dict[str, str]
) -> None:
    body = {"name": " Main ", "location_code": " kyiv-01 ", "address": "Kyiv, Khreshchatyk 1"}

    # Create (admin) with normalization; storekeeper can't.
    created = await client.post("/warehouses", json=body, headers=admin_headers)
    assert created.status_code == 201
    main = created.json()
    assert (main["name"], main["location_code"], main["is_active"]) == ("Main", "KYIV-01", True)
    assert (
        await client.post("/warehouses", json=body, headers=storekeeper_headers)
    ).status_code == 403

    # Duplicate code (any case) -> 409 via ON CONFLICT; bad code -> 422.
    dup = await client.post(
        "/warehouses", json={**body, "location_code": "KYIV-01"}, headers=admin_headers
    )
    assert dup.status_code == 409 and dup.json()["code"] == "location_code_taken"
    bad = await client.post(
        "/warehouses", json={**body, "location_code": "has space"}, headers=admin_headers
    )
    assert bad.status_code == 422

    other = (
        await client.post(
            "/warehouses",
            json={"name": "Reserve", "location_code": "LVIV-01", "address": "Lviv"},
            headers=admin_headers,
        )
    ).json()

    # Read: any authenticated user; anonymous is rejected.
    one = await client.get(f"/warehouses/{main['id']}", headers=storekeeper_headers)
    assert one.status_code == 200 and one.json() == main
    assert (await client.get("/warehouses")).status_code == 401
    missing = await client.get("/warehouses/999999", headers=storekeeper_headers)
    assert missing.status_code == 404 and missing.json()["code"] == "warehouse_not_found"

    # Patch (admin): rename + deactivate; storekeeper can't; code clash -> 409; empty -> 422.
    url = f"/warehouses/{other['id']}"
    patched = await client.patch(
        url, json={"name": "Reserve B", "is_active": False}, headers=admin_headers
    )
    assert patched.status_code == 200
    assert (patched.json()["name"], patched.json()["is_active"]) == ("Reserve B", False)
    assert (
        await client.patch(url, json={"name": "x"}, headers=storekeeper_headers)
    ).status_code == 403
    clash = await client.patch(url, json={"location_code": "kyiv-01"}, headers=admin_headers)
    assert clash.status_code == 409 and clash.json()["code"] == "location_code_taken"
    assert (await client.patch(url, json={}, headers=admin_headers)).status_code == 422
    assert (
        await client.patch("/warehouses/999999", json={"name": "x"}, headers=admin_headers)
    ).status_code == 404

    # List: paginated, filterable by is_active.
    all_ = (await client.get("/warehouses", headers=storekeeper_headers)).json()
    assert [w["id"] for w in all_["items"]] == [main["id"], other["id"]] and all_["total"] == 2
    inactive = (await client.get("/warehouses?is_active=false", headers=storekeeper_headers)).json()
    assert [w["id"] for w in inactive["items"]] == [other["id"]] and inactive["total"] == 1
