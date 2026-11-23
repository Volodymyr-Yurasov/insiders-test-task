from pydantic_settings import BaseSettings, SettingsConfigDict


class NotificationsConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # example.com is reserved for documentation: nothing real is ever addressed by default.
    LOW_STOCK_ALERT_EMAIL: str = "inventory-alerts@example.com"


notifications_settings = NotificationsConfig()
