from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ReportsConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Safety net only: invalidation is explicit; the TTL bounds staleness if an INCR is lost.
    VALUATION_CACHE_TTL_S: int = Field(default=300, ge=1)
    VALUATION_LOCK_TTL_S: int = Field(default=5, ge=1)
    # How long a request that lost the recompute lock waits for the winner's result.
    VALUATION_LOCK_WAIT_S: float = Field(default=1.0, ge=0)
    VALUATION_LOCK_POLL_S: float = Field(default=0.05, gt=0)


reports_settings = ReportsConfig()
