from pydantic import PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.constants import Environment


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: PostgresDsn = PostgresDsn(
        "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory"
    )
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 10

    REDIS_URL: RedisDsn = RedisDsn("redis://localhost:6379/0")

    # Safe default: docs are hidden unless an environment explicitly opts in.
    ENVIRONMENT: Environment = Environment.PRODUCTION

    HEALTH_CHECK_TIMEOUT_S: float = 2.0

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def _force_asyncpg_driver(cls, value: object) -> object:
        # Accept plain `postgresql://` / `postgres://` URLs (compose, .env, PaaS) and pin the
        # async driver the app is built on.
        if isinstance(value, str):
            for prefix in ("postgresql://", "postgres://"):
                if value.startswith(prefix):
                    return "postgresql+asyncpg://" + value.removeprefix(prefix)
        return value


settings = Config()
