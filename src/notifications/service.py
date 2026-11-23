"""Low-stock notifications (spec 2.5). Sending an email is simulated by logging it."""

import logging
from collections.abc import Sequence
from typing import Protocol

from src.notifications.config import notifications_settings
from src.notifications.schemas import LowStockAlert

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    async def send_low_stock(self, alerts: Sequence[LowStockAlert]) -> None: ...


class LoggingNotifier:
    """Stands in for an email gateway: one 'email sent' log line per alert."""

    async def send_low_stock(self, alerts: Sequence[LowStockAlert]) -> None:
        recipient = notifications_settings.LOW_STOCK_ALERT_EMAIL
        for alert in alerts:
            logger.info(
                "email sent to %s: low stock for %s (%s) in warehouse %s: %s -> %s "
                "(min %s, operation %s)",
                recipient,
                alert.sku,
                alert.product_name,
                alert.warehouse_id,
                alert.stock_before,
                alert.stock_after,
                alert.min_stock_level,
                alert.operation_id,
            )


async def safe_send(notifier: Notifier, alerts: Sequence[LowStockAlert]) -> None:
    """Background-task entry point. Never raises: the client already got its 2xx, so a
    failing mail gateway must be logged, not re-raised into the server."""
    try:
        await notifier.send_low_stock(alerts)
    except Exception:
        logger.exception("low-stock notification failed for %d alert(s)", len(alerts))
