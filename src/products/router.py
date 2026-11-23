from typing import Any

from fastapi import APIRouter, Depends, status

from src.auth.dependencies import admin_user, current_user
from src.database import DbSession
from src.products import service as products_service
from src.products.dependencies import ValidProduct
from src.products.models import Product
from src.products.schemas import ProductCreate, ProductDetail, ProductRead, ProductUpdate

# Every route needs a logged-in user; write routes additionally require admin.
router = APIRouter(prefix="/products", tags=["products"], dependencies=[Depends(current_user)])
ADMIN_ONLY = [Depends(admin_user)]
NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"description": "Product not found (or soft-deleted)"}
}


@router.post(
    "",
    response_model=ProductRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=ADMIN_ONLY,
    summary="Create a product (admin)",
    responses={409: {"description": "SKU already exists, possibly on a deleted product"}},
)
async def create_product(body: ProductCreate, db: DbSession) -> Product:
    return await products_service.create_product(db, body)


@router.get(
    "/{product_id}",
    response_model=ProductDetail,
    summary="Get a product with its total and per-warehouse stock",
    responses=NOT_FOUND,
)
async def get_product(product_id: int, db: DbSession) -> ProductDetail:
    # No ValidProduct dependency: the detail query itself 404s, keeping this ONE statement.
    return await products_service.get_product_detail(db, product_id)


@router.patch(
    "/{product_id}",
    response_model=ProductRead,
    dependencies=ADMIN_ONLY,
    summary="Edit name, base price or min stock level (admin; SKU is immutable)",
    responses=NOT_FOUND,
)
async def update_product(product: ValidProduct, body: ProductUpdate, db: DbSession) -> Product:
    return await products_service.update_product(db, product, body)


@router.delete(
    "/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=ADMIN_ONLY,
    summary="Soft-delete a product (admin)",
    responses=NOT_FOUND,
)
async def delete_product(product: ValidProduct, db: DbSession) -> None:
    await products_service.soft_delete_product(db, product)
