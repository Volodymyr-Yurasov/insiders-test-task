from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.products.exceptions import SkuTaken
from src.products.models import Product
from src.products.schemas import ProductCreate, ProductUpdate


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
    # Step 4 adds the D8 guard here: lock the row, allow only when total stock is 0.
    product.is_deleted = True
    await db.commit()
    # Step 6: invalidate valuation cache (product leaves the report).
