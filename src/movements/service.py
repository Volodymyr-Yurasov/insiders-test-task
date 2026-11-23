"""The movement engine. Single movements and batches share this one code path (D2).

Concurrency design (spec 2.1), all inside ONE transaction under READ COMMITTED:
  1. warehouses involved:  SELECT ... ORDER BY id FOR SHARE   (blocks concurrent deactivation)
  2. products involved:    SELECT ... ORDER BY id FOR UPDATE  (OUT/TRANSFER; FOR SHARE for IN)
  3. source stock:         one grouped SUM, run AFTER the locks, so it's a fresh snapshot
                           that includes whatever the previous lock holder committed
  4. any shortage -> raise (nothing written; the session rolls back) -- all-or-nothing (2.2)
  5. insert operation + movements, commit (releases the locks)
Locks are always taken warehouses -> products, each in id order: no deadlock cycles.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.exceptions import ServiceBusy
from src.movements import stock
from src.movements.config import movements_settings
from src.movements.constants import MovementType
from src.movements.exceptions import InsufficientStock, ProductsNotFound, WarehouseInactive
from src.movements.models import Operation, StockMovement
from src.movements.schemas import BatchCreate, MovementCreate, MovementRead, OperationRead
from src.notifications.schemas import LowStockAlert
from src.pagination import PageParams, paginate
from src.products.models import Product
from src.warehouses.exceptions import WarehouseNotFound
from src.warehouses.models import Warehouse

# lock_not_available (lock_timeout hit) and deadlock_detected: both are "retry shortly".
_RETRYABLE_SQLSTATES = {"55P03", "40P01"}


@dataclass(frozen=True)
class Command:
    type: MovementType
    from_warehouse_id: int | None
    to_warehouse_id: int | None
    items: list[tuple[str, int]]  # (sku, quantity), SKUs unique

    @classmethod
    def single(cls, body: MovementCreate) -> "Command":
        return cls(
            body.type, body.from_warehouse_id, body.to_warehouse_id, [(body.sku, body.quantity)]
        )

    @classmethod
    def batch(cls, body: BatchCreate) -> "Command":
        items = [(item.sku, item.quantity) for item in body.items]
        return cls(body.type, body.from_warehouse_id, body.to_warehouse_id, items)


@dataclass(frozen=True)
class LockedProduct:
    id: int
    name: str
    min_stock_level: int


@dataclass(frozen=True)
class ExecutionResult:
    operation: OperationRead
    # Source-warehouse threshold crossings caused by this operation (spec 2.5, D6).
    low_stock: list[LowStockAlert]


async def execute(db: AsyncSession, user_id: int, command: Command) -> ExecutionResult:
    try:
        return await _execute(db, user_id, command)
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) in _RETRYABLE_SQLSTATES:
            await db.rollback()
            raise ServiceBusy() from exc
        raise


async def _execute(db: AsyncSession, user_id: int, command: Command) -> ExecutionResult:
    # Bound every lock wait in this transaction (int(): never interpolate untrusted text).
    await db.execute(
        text(f"SET LOCAL lock_timeout = '{int(movements_settings.LOCK_TIMEOUT_MS)}ms'")
    )

    await _lock_active_warehouses(db, command)
    products = await _lock_products(db, command)
    product_ids = {sku: product.id for sku, product in products.items()}

    # Stock in the source warehouse, read under the locks: `before` for the shortage check
    # and for low-stock crossings (no extra query).
    before: dict[int, int] = {}
    if command.type.decreases_source:
        assert command.from_warehouse_id is not None
        before = await stock.stock_in_warehouse(db, command.from_warehouse_id, product_ids.values())
        shortages = [
            {
                "sku": sku,
                "requested": quantity,
                "available": before[product_ids[sku]],
                "missing": quantity - before[product_ids[sku]],
            }
            for sku, quantity in command.items
            if quantity > before[product_ids[sku]]
        ]
        if shortages:
            raise InsufficientStock(shortages=shortages)

    operation_id, created_at = (
        await db.execute(
            insert(Operation)
            .values(type=command.type, performed_by=user_id)
            .returning(Operation.id, Operation.created_at)
        )
    ).one()
    inserted = await db.execute(
        insert(StockMovement)
        .values(
            [
                {
                    "operation_id": operation_id,
                    "type": command.type,
                    "product_id": product_ids[sku],
                    "quantity": quantity,
                    "from_warehouse_id": command.from_warehouse_id,
                    "to_warehouse_id": command.to_warehouse_id,
                    "performed_by": user_id,
                }
                for sku, quantity in command.items
            ]
        )
        .returning(StockMovement.id, StockMovement.product_id)
    )
    movement_ids = {product_id: movement_id for movement_id, product_id in inserted}
    await db.commit()
    # Post-commit side effects (cache invalidation, notifications) are run by the router.

    operation = OperationRead(
        id=operation_id,
        type=command.type,
        performed_by=user_id,
        created_at=created_at,
        movements=[
            MovementRead(
                id=movement_ids[product_ids[sku]],
                product_id=product_ids[sku],
                sku=sku,
                quantity=quantity,
                from_warehouse_id=command.from_warehouse_id,
                to_warehouse_id=command.to_warehouse_id,
            )
            for sku, quantity in command.items
        ],
    )
    return ExecutionResult(operation, _low_stock_crossings(command, products, before, operation_id))


def _low_stock_crossings(
    command: Command,
    products: dict[str, LockedProduct],
    before: dict[int, int],
    operation_id: int,
) -> list[LowStockAlert]:
    """Alert only when this operation crosses the threshold (before >= min > after), so
    further write-offs below it don't re-alert (D6). Computed from the locked read, hence
    exactly one of N concurrent write-offs is the one that crosses."""
    if not command.type.decreases_source:
        return []
    assert command.from_warehouse_id is not None
    alerts = []
    for sku, quantity in command.items:
        product = products[sku]
        stock_before = before[product.id]
        stock_after = stock_before - quantity
        if stock_before >= product.min_stock_level > stock_after:
            alerts.append(
                LowStockAlert(
                    operation_id=operation_id,
                    product_id=product.id,
                    sku=sku,
                    product_name=product.name,
                    warehouse_id=command.from_warehouse_id,
                    stock_before=stock_before,
                    stock_after=stock_after,
                    min_stock_level=product.min_stock_level,
                )
            )
    return alerts


async def _lock_active_warehouses(db: AsyncSession, command: Command) -> None:
    ids = sorted({w for w in (command.from_warehouse_id, command.to_warehouse_id) if w is not None})
    rows = (
        await db.execute(
            select(Warehouse.id, Warehouse.is_active)
            .where(Warehouse.id.in_(ids))
            .order_by(Warehouse.id)
            .with_for_update(read=True)
        )
    ).all()
    active = {warehouse_id: is_active for warehouse_id, is_active in rows}
    for warehouse_id in ids:
        if warehouse_id not in active:
            raise WarehouseNotFound(warehouse_id=warehouse_id)
        if not active[warehouse_id]:
            raise WarehouseInactive(warehouse_id=warehouse_id)


async def _lock_products(db: AsyncSession, command: Command) -> dict[str, LockedProduct]:
    """Lock the batch's products in id order; return sku -> product. All unknown SKUs -> 404."""
    skus = [sku for sku, _ in command.items]
    rows = (
        await db.execute(
            select(Product.sku, Product.id, Product.name, Product.min_stock_level)
            .where(Product.sku.in_(skus), Product.is_deleted.is_(False))
            .order_by(Product.id)
            # Decrements need exclusive locks; IN only has to block a concurrent soft-delete
            # (an FK insert alone takes KEY SHARE, which doesn't conflict with that UPDATE).
            .with_for_update(read=not command.type.decreases_source)
        )
    ).all()
    products = {sku: LockedProduct(id_, name, min_level) for sku, id_, name, min_level in rows}
    missing = [sku for sku in skus if sku not in products]
    if missing:
        raise ProductsNotFound(missing_skus=missing)
    return products


async def list_movements(
    db: AsyncSession,
    page: PageParams,
    *,
    product_id: int | None = None,
    warehouse_id: int | None = None,
    type_: MovementType | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict[str, Any]:
    """Movement history, newest first; `warehouse_id` matches either side, `date_to` exclusive."""
    stmt = (
        select(
            StockMovement.id,
            StockMovement.operation_id,
            StockMovement.type,
            StockMovement.product_id,
            Product.sku,
            StockMovement.quantity,
            StockMovement.from_warehouse_id,
            StockMovement.to_warehouse_id,
            StockMovement.performed_by,
            StockMovement.created_at,
        )
        .join(Product, Product.id == StockMovement.product_id)
        .order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
    )
    if product_id is not None:
        stmt = stmt.where(StockMovement.product_id == product_id)
    if warehouse_id is not None:
        stmt = stmt.where(stock.touches_warehouse(warehouse_id))
    if type_ is not None:
        stmt = stmt.where(StockMovement.type == type_)
    if date_from is not None:
        stmt = stmt.where(StockMovement.created_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(StockMovement.created_at < date_to)
    return await paginate(db, stmt, page)
