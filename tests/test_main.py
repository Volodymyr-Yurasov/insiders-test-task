import pytest
from httpx import ASGITransport, AsyncClient

from src.config import settings
from src.constants import Environment
from src.main import create_app


@pytest.mark.parametrize(
    ("environment", "expected_status"),
    [(Environment.LOCAL, 200), (Environment.PRODUCTION, 404)],
)
async def test_docs_visible_only_outside_production(
    monkeypatch: pytest.MonkeyPatch, environment: Environment, expected_status: int
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", environment)
    app = create_app()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            for path in ("/docs", "/openapi.json"):
                assert (await c.get(path)).status_code == expected_status, path
    finally:
        await app.state.redis.aclose()
        await app.state.engine.dispose()
