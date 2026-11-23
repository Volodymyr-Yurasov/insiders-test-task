"""Demo data for reviewers: `python -m src.seed`

In compose: `docker compose exec api python -m src.seed`.

Idempotent: users/warehouses/products are created only if missing, demo movements only when
the ledger is empty. The first admin comes from FIRST_ADMIN_* (seeded at app startup).

Demo only: refuses to run when ENVIRONMENT=production. The storekeeper login is created only
when SEED_STOREKEEPER_PASSWORD is set (never hardcoded, never logged).
"""

import asyncio
import logging
import sys
from decimal import Decimal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import service as auth_service
from src.auth.constants import Role
from src.auth.models import AppUser
from src.config import settings
from src.constants import Environment
from src.database import create_engine, create_session_factory
from src.movements import service as movements_service
from src.movements.constants import MovementType
from src.movements.models import StockMovement
from src.products import service as products_service
from src.products.exceptions import SkuTaken
from src.products.models import Product
from src.products.schemas import ProductCreate
from src.redis import create_redis
from src.reports import cache as valuation_cache
from src.warehouses import service as warehouses_service
from src.warehouses.exceptions import LocationCodeTaken
from src.warehouses.models import Warehouse
from src.warehouses.schemas import WarehouseCreate

logger = logging.getLogger("seed")


class SeedConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    SEED_STOREKEEPER_USERNAME: str = "storekeeper"
    SEED_STOREKEEPER_PASSWORD: SecretStr | None = None


WAREHOUSES = [
    WarehouseCreate(name="Kyiv central", location_code="KYIV-01", address="Kyiv, Hlybochytska 1"),
    WarehouseCreate(name="Lviv west", location_code="LVIV-01", address="Lviv, Horodotska 100"),
]
PRODUCTS = [
    ProductCreate(
        sku="BOLT-M8", name="Bolt M8x40", base_price=Decimal("0.25"), min_stock_level=100
    ),
    ProductCreate(sku="NUT-M8", name="Nut M8", base_price=Decimal("0.10"), min_stock_level=100),
    ProductCreate(
        sku="DRILL-500", name="Drill 500W", base_price=Decimal("1499.00"), min_stock_level=2
    ),
    ProductCreate(
        sku="GLOVES-L", name="Work gloves L", base_price=Decimal("35.50"), min_stock_level=20
    ),
]


async def _warehouse(db: AsyncSession, data: WarehouseCreate) -> Warehouse:
    try:
        return await warehouses_service.create_warehouse(db, data)
    except LocationCodeTaken:
        return (
            await db.execute(select(Warehouse).where(Warehouse.location_code == data.location_code))
        ).scalar_one()


async def _product(db: AsyncSession, data: ProductCreate) -> Product:
    try:
        return await products_service.create_product(db, data)
    except SkuTaken:
        return (await db.execute(select(Product).where(Product.sku == data.sku))).scalar_one()


async def seed() -> None:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            await auth_service.ensure_first_admin(db)
            seed_config = SeedConfig()
            username = seed_config.SEED_STOREKEEPER_USERNAME
            if seed_config.SEED_STOREKEEPER_PASSWORD is None:
                logger.info("SEED_STOREKEEPER_PASSWORD not set: no storekeeper login created")
            elif await auth_service.create_user(
                db,
                username=username,
                password=seed_config.SEED_STOREKEEPER_PASSWORD.get_secret_value(),
                role=Role.STOREKEEPER,
            ):
                logger.info("created user %r (password from SEED_STOREKEEPER_PASSWORD)", username)
            kyiv, lviv = [await _warehouse(db, w) for w in WAREHOUSES]
            for product in PRODUCTS:
                await _product(db, product)

            if await db.scalar(select(func.count()).select_from(StockMovement)):
                logger.info("ledger not empty: demo movements skipped")
                return
            # Movements need a performer: the storekeeper if present, otherwise any admin.
            keeper = await auth_service.get_by_username(db, username) or await db.scalar(
                select(AppUser).where(AppUser.role == Role.ADMIN).order_by(AppUser.id).limit(1)
            )
            if keeper is None:
                logger.error("no user to record demo movements as; set FIRST_ADMIN_* first")
                return
            demo = [
                (
                    MovementType.IN,
                    None,
                    kyiv.id,
                    [("BOLT-M8", 500), ("NUT-M8", 500), ("DRILL-500", 5)],
                ),
                (MovementType.IN, None, lviv.id, [("GLOVES-L", 40), ("BOLT-M8", 50)]),
                (MovementType.TRANSFER, kyiv.id, lviv.id, [("BOLT-M8", 120), ("DRILL-500", 2)]),
                (MovementType.OUT, kyiv.id, None, [("NUT-M8", 450)]),  # NUT-M8 now below min
            ]
            for type_, src, dst, items in demo:
                await movements_service.execute(
                    db, keeper.id, movements_service.Command(type_, src, dst, items)
                )
            logger.info("demo movements recorded")
            # Written outside the HTTP routes, so invalidate the valuation cache here too.
            redis = create_redis(settings)
            try:
                await valuation_cache.invalidate(redis)
            finally:
                await redis.aclose()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if settings.ENVIRONMENT == Environment.PRODUCTION:
        logger.error("refusing to seed demo data into a production environment")
        sys.exit(1)
    asyncio.run(seed())
