"""Spec 2.5: low-stock email via BackgroundTasks when a write-off crosses min_stock_level."""

import asyncio
import logging
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from src.notifications.dependencies import get_notifier
from src.notifications.schemas import LowStockAlert
from src.notifications.service import LoggingNotifier
from tests.conftest import MakeProduct, MakeWarehouse


class RecordingNotifier:
    def __init__(self) -> None:
        self.batches: list[list[LowStockAlert]] = []

    async def send_low_stock(self, alerts: Sequence[LowStockAlert]) -> None:
        self.batches.append(list(alerts))

    def crossings(self) -> list[tuple[str, int, int, int]]:
        return [
            (a.sku, a.warehouse_id, a.stock_before, a.stock_after)
            for batch in self.batches
            for a in batch
        ]


class FailingNotifier:
    async def send_low_stock(self, alerts: Sequence[LowStockAlert]) -> None:
        raise ConnectionError("SMTP server down")


@pytest.fixture
def notifier(app: FastAPI) -> Iterator[RecordingNotifier]:
    recording = RecordingNotifier()
    app.dependency_overrides[get_notifier] = lambda: recording
    yield recording
    app.dependency_overrides.pop(get_notifier, None)


async def test_low_stock_notifications(
    app: FastAPI,
    client: AsyncClient,
    storekeeper_headers: dict[str, str],
    make_warehouse: MakeWarehouse,
    make_product: MakeProduct,
    notifier: RecordingNotifier,
) -> None:
    w1, w2 = await make_warehouse(), await make_warehouse()
    bolt = await make_product("BOLT", min_stock_level=5)
    nut = await make_product("NUT", min_stock_level=3)

    async def move(**body: Any) -> int:
        path = "/movements/batch" if "items" in body else "/movements"
        return (await client.post(path, json=body, headers=storekeeper_headers)).status_code

    assert await move(type="IN", sku="BOLT", quantity=6, to_warehouse_id=w1.id) == 201
    assert notifier.crossings() == [], "IN never alerts"

    # 6 -> 4 crosses min=5: one alert with before/after; 4 -> 3 is already below: no re-alert.
    assert await move(type="OUT", sku="BOLT", quantity=2, from_warehouse_id=w1.id) == 201
    assert notifier.crossings() == [("BOLT", w1.id, 6, 4)]
    assert await move(type="OUT", sku="BOLT", quantity=1, from_warehouse_id=w1.id) == 201
    assert len(notifier.crossings()) == 1

    # A failed (rolled back) write-off never notifies.
    assert await move(type="OUT", sku="BOLT", quantity=99, from_warehouse_id=w1.id) == 409
    assert len(notifier.crossings()) == 1

    # TRANSFER depletes the source warehouse too (per-warehouse threshold).
    assert await move(type="IN", sku="BOLT", quantity=10, to_warehouse_id=w1.id) == 201  # 13
    assert (
        await move(
            type="TRANSFER", sku="BOLT", quantity=9, from_warehouse_id=w1.id, to_warehouse_id=w2.id
        )
        == 201
    )
    assert notifier.crossings()[-1] == ("BOLT", w1.id, 13, 4)

    # One batch crossing two thresholds -> one background task with two alerts.
    assert await move(type="IN", sku="NUT", quantity=3, to_warehouse_id=w2.id) == 201
    batch = {
        "type": "OUT",
        "from_warehouse_id": w2.id,
        "items": [
            {"sku": "BOLT", "quantity": 5},  # 9 -> 4 crosses 5
            {"sku": "NUT", "quantity": 1},  # 3 -> 2 crosses 3
        ],
    }
    assert await move(**batch) == 201
    assert [(a.sku, a.stock_before, a.stock_after) for a in notifier.batches[-1]] == [
        ("BOLT", 9, 4),
        ("NUT", 3, 2),
    ]
    assert {a.product_id for a in notifier.batches[-1]} == {bolt.id, nut.id}


async def test_concurrent_write_offs_alert_exactly_once(
    client: AsyncClient,
    storekeeper_headers: dict[str, str],
    make_warehouse: MakeWarehouse,
    make_product: MakeProduct,
    notifier: RecordingNotifier,
) -> None:
    """The crossing is computed from the locked stock read: one of N racers crosses."""
    warehouse, product = await make_warehouse(), await make_product(min_stock_level=5)
    stock_in = {"type": "IN", "sku": product.sku, "quantity": 10, "to_warehouse_id": warehouse.id}
    assert (
        await client.post("/movements", json=stock_in, headers=storekeeper_headers)
    ).status_code == 201

    out = {"type": "OUT", "sku": product.sku, "quantity": 1, "from_warehouse_id": warehouse.id}
    results = await asyncio.gather(
        *(client.post("/movements", json=out, headers=storekeeper_headers) for _ in range(10))
    )
    assert all(r.status_code == 201 for r in results)
    assert notifier.crossings() == [(product.sku, warehouse.id, 5, 4)]


async def test_failing_notifier_never_breaks_the_request(
    app: FastAPI,
    client: AsyncClient,
    storekeeper_headers: dict[str, str],
    make_warehouse: MakeWarehouse,
    make_product: MakeProduct,
    caplog: pytest.LogCaptureFixture,
) -> None:
    warehouse, product = await make_warehouse(), await make_product(min_stock_level=5)
    stock_in = {"type": "IN", "sku": product.sku, "quantity": 5, "to_warehouse_id": warehouse.id}
    assert (
        await client.post("/movements", json=stock_in, headers=storekeeper_headers)
    ).status_code == 201

    app.dependency_overrides[get_notifier] = FailingNotifier
    try:
        with caplog.at_level(logging.ERROR, logger="src.notifications.service"):
            out = {
                "type": "OUT",
                "sku": product.sku,
                "quantity": 1,
                "from_warehouse_id": warehouse.id,
            }
            r = await client.post("/movements", json=out, headers=storekeeper_headers)
    finally:
        app.dependency_overrides.pop(get_notifier, None)

    assert r.status_code == 201
    assert "low-stock notification failed" in caplog.text


async def test_logging_notifier_logs_the_email(caplog: pytest.LogCaptureFixture) -> None:
    alert = LowStockAlert(
        operation_id=1,
        product_id=2,
        sku="BOLT",
        product_name="Bolt",
        warehouse_id=3,
        stock_before=6,
        stock_after=4,
        min_stock_level=5,
    )
    with caplog.at_level(logging.INFO, logger="src.notifications.service"):
        await LoggingNotifier().send_low_stock([alert])
    assert "email sent to inventory-alerts@example.com" in caplog.text
    assert "BOLT" in caplog.text and "6 -> 4" in caplog.text
