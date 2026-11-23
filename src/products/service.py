from sqlalchemy import JSON, func, literal_column, select
from sqlalchemy.dialects.postgresql import aggregate_order_by, insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.movements import stock as movements_stock
from src.movements.models import StockMovement
from src.products.exceptions import ProductHasStock, ProductNotFound, SkuTaken
from src.products.models import Product
from src.products.schemas import ProductCreate, ProductDetail, ProductRead, ProductUpdate
from src.warehouses.models import Warehouse


async def get_product_detail(db: AsyncSession, product_id: int) -> ProductDetail:
    """Product + total stock + per-warehouse breakdown in ONE statement (C11).

    Deleted products are 404 like everywhere else.
    """
    ledger = movements_stock.signed_ledger()
    per_warehouse = (
        select(ledger.c.warehouse_id, func.sum(ledger.c.delta).label("quantity"))
        .where(ledger.c.product_id == product_id)
        .group_by(ledger.c.warehouse_id)
        .having(func.sum(ledger.c.delta) != 0)
        .subquery("per_warehouse")
    )
    breakdown = (
        select(
            func.coalesce(
                func.json_agg(
                    aggregate_order_by(
                        func.json_build_object(
                            "warehouse_id",
                            Warehouse.id,
                            "location_code",
                            Warehouse.location_code,
                            "quantity",
                            per_warehouse.c.quantity,
                        ),
                        Warehouse.id,
                    )
                ),
                literal_column("'[]'::json"),
                type_=JSON,
            )
        )
        .select_from(per_warehouse.join(Warehouse, Warehouse.id == per_warehouse.c.warehouse_id))
        .scalar_subquery()
    )
    total = (
        select(movements_stock.signed_total())
        .where(StockMovement.product_id == product_id)
        .scalar_subquery()
    )
    row = (
        await db.execute(
            select(
                Product, total.label("total_stock"), breakdown.label("stock_by_warehouse")
            ).where(Product.id == product_id, Product.is_deleted.is_(False))
        )
    ).one_or_none()
    if row is None:
        raise ProductNotFound()
    product, total_stock, stock_by_warehouse = row
    return ProductDetail(
        **ProductRead.model_validate(product).model_dump(),
        total_stock=total_stock,
        stock_by_warehouse=stock_by_warehouse,
    )


async def create_product(db: AsyncSession, data: ProductCreate) -> Product:
    # ON CONFLICT, not a pre-check (races) nor IntegrityError (aborts the transaction).
    # The unique index covers soft-deleted rows, so a deleted SKU conflicts too (D9).
    product = (
        await db.execute(
            insert(Product)
            .values(**data.model_dump())
            .on_conflict_do_nothing(index_elements=[Product.sku])
            .returning(Product)
        )
    ).scalar_one_or_none()
    if product is None:
        raise SkuTaken()
    await db.commit()
    # Step 6: invalidate valuation cache (new product).
    return product


async def update_product(db: AsyncSession, product: Product, changes: ProductUpdate) -> Product:
    # No unique column is editable (SKU is immutable), so no IntegrityError path here.
    for field, value in changes.changes().items():
        setattr(product, field, value)
    await db.commit()
    # Step 6: invalidate valuation cache (base_price affects the report).
    return product


async def soft_delete_product(db: AsyncSession, product: Product) -> None:
    """D8: only a product with zero total stock can be deleted.

    The row lock conflicts with the movement engine's product lock (FOR SHARE for IN,
    FOR UPDATE for OUT/TRANSFER), so no movement can land between the check and the flag.
    """
    already_deleted = await db.scalar(
        select(Product.is_deleted).where(Product.id == product.id).with_for_update()
    )
    if already_deleted:  # a concurrent DELETE won the lock first
        raise ProductNotFound()
    if await movements_stock.product_total_stock(db, product.id) != 0:
        raise ProductHasStock()
    product.is_deleted = True
    await db.commit()
    # Step 6: invalidate valuation cache (product leaves the report).
