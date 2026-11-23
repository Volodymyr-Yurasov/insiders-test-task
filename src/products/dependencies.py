from typing import Annotated

from fastapi import Depends

from src.database import DbSession
from src.products.exceptions import ProductNotFound
from src.products.models import Product
from src.schemas import IdPath


async def valid_product_id(product_id: IdPath, db: DbSession) -> Product:
    # Soft-deleted products look gone everywhere.
    product = await db.get(Product, product_id)
    if product is None or product.is_deleted:
        raise ProductNotFound()
    return product


ValidProduct = Annotated[Product, Depends(valid_product_id)]
