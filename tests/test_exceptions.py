import json

from httpx import AsyncClient

from src.exceptions import unhandled_exception_handler


async def test_error_format(client: AsyncClient, admin_headers: dict[str, str]) -> None:
    unknown = await client.get("/no-such-route")
    assert unknown.status_code == 404
    assert unknown.json() == {"code": "not_found", "detail": "Not Found"}

    wrong_method = await client.delete("/health")
    assert wrong_method.status_code == 405
    assert wrong_method.json()["code"] == "method_not_allowed"
    assert "GET" in wrong_method.headers["allow"]

    invalid = await client.post(
        "/users",
        headers=admin_headers,
        json={"username": "ok-name", "password": "secret-but-short"[:5], "role": "storekeeper"},
    )
    assert invalid.status_code == 422
    body = invalid.json()
    assert body["code"] == "validation_error"
    assert body["detail"] == "Request validation failed"
    assert [(e["loc"], e["type"]) for e in body["errors"]] == [
        (["body", "password"], "string_too_short")
    ]
    # Submitted values are never echoed back (no Pydantic `input`).
    assert "secre" not in invalid.text


async def test_unhandled_exception_is_opaque() -> None:
    # Called directly: through ASGITransport the server error middleware re-raises.
    response = await unhandled_exception_handler(None, RuntimeError("db password=hunter2"))  # type: ignore[arg-type]
    assert response.status_code == 500
    assert json.loads(bytes(response.body)) == {
        "code": "internal_error",
        "detail": "Internal server error",
    }
