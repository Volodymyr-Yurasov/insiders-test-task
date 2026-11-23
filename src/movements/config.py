from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MovementsConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Max wait for a row lock before answering 503 instead of hanging the request.
    LOCK_TIMEOUT_MS: int = Field(default=5000, ge=1)


movements_settings = MovementsConfig()
