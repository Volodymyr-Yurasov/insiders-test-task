from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RateLimitConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Bonus spec: no more than 50 transactions per minute per employee.
    RATE_LIMIT_MAX_REQUESTS: int = Field(default=50, ge=1)
    RATE_LIMIT_WINDOW_S: float = Field(default=60, gt=0)


rate_limit_settings = RateLimitConfig()
