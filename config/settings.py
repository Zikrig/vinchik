from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str = Field(alias="BOT_TOKEN")
    database_url: str = Field(alias="DATABASE_URL")
    redis_url: str = Field(alias="REDIS_URL")
    admin_ids: str = Field(alias="ADMIN_IDS")
    admin_web_password: str = Field(alias="ADMIN_WEB_PASSWORD")
    manager_contact: str = Field(default="@manager", alias="MANAGER_CONTACT")
    payment_card: str = Field(default="", alias="PAYMENT_CARD")
    payment_check_time: str = Field(default="в течение 24 часов", alias="PAYMENT_CHECK_TIME")
    bot_username: str = Field(default="", alias="BOT_USERNAME")
    web_secret_key: str = Field(alias="WEB_SECRET_KEY")
    web_host: str = Field(default="0.0.0.0", alias="WEB_HOST")
    web_port: int = Field(default=8080, alias="WEB_PORT")

    use_webhook: bool = Field(default=False, alias="USE_WEBHOOK")
    webhook_base_url: str = Field(default="", alias="WEBHOOK_BASE_URL")
    webhook_path: str = Field(default="/webhook/bot", alias="WEBHOOK_PATH")
    webhook_secret: str = Field(default="", alias="WEBHOOK_SECRET")
    webhook_host: str = Field(default="0.0.0.0", alias="WEBHOOK_HOST")
    webhook_port: int = Field(default=8081, alias="WEBHOOK_PORT")

    default_daily_like_limit: int = 50
    default_max_distance_km: float = 100.0
    like_notify_interval_minutes: int = 30
    registration_only_default: bool = True

    @property
    def admin_id_set(self) -> set[int]:
        if not self.admin_ids.strip():
            return set()
        return {int(x.strip()) for x in self.admin_ids.split(",") if x.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
