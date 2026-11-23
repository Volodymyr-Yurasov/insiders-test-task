from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Required, no default: a missing/weak secret must fail at startup, not at first login.
    JWT_SECRET: SecretStr = Field(min_length=32)
    JWT_ALG: str = "HS256"
    JWT_ACCESS_EXP_MINUTES: int = 15
    JWT_REFRESH_EXP_DAYS: int = 14

    # Optional first-admin seed (both must be set; empty values mean "skip").
    FIRST_ADMIN_USERNAME: str | None = None
    FIRST_ADMIN_PASSWORD: SecretStr | None = None

    @field_validator("FIRST_ADMIN_USERNAME", "FIRST_ADMIN_PASSWORD", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        # docker compose passes `${VAR:-}` as an empty string when the var isn't set.
        return None if value == "" else value

    @field_validator("FIRST_ADMIN_PASSWORD")
    @classmethod
    def _seed_password_strength(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < 8:
            raise ValueError("FIRST_ADMIN_PASSWORD must be at least 8 characters")
        return value


auth_settings = AuthConfig()  # JWT_SECRET comes from env
