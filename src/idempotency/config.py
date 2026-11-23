from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class IdempotencyConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # How long a completed result is replayed from Redis (bonus spec: 24 hours).
    IDEMPOTENCY_TTL_S: int = Field(default=86_400, ge=1)
    # Marker while the first request runs; well above the 5 s DB lock timeout. If it ever
    # expires mid-flight, the DB unique constraint still guarantees exactly-once.
    IDEMPOTENCY_PROCESSING_TTL_S: int = Field(default=30, ge=1)


idempotency_settings = IdempotencyConfig()
