"""
Centralized application settings.

Everything that changes between a laptop, CI, and production is read from the
environment. Typed settings mean a bad value fails at startup rather than at
3am when a section finally opens and the notifier cannot reach SMTP.
"""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = Field(default="development", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    database_url: str = Field(
        default="postgresql+psycopg://postgres:postgres@localhost:5432/seatalerts",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    schedule_base_url: str = Field(
        default="https://example.edu/course-schedule", alias="SCHEDULE_BASE_URL"
    )
    schedule_user_agent: str = Field(
        default="class-seat-alerts/1.0 (student project)", alias="SCHEDULE_USER_AGENT"
    )
    schedule_timeout_seconds: float = Field(default=15.0, alias="SCHEDULE_TIMEOUT_SECONDS")

    # The school's servers are not mine to hammer. This cap applies across every
    # worker, which is why it is enforced in Redis rather than per-process.
    scrape_requests_per_window: int = Field(default=30, alias="SCRAPE_REQUESTS_PER_WINDOW")
    scrape_window_seconds: int = Field(default=60, alias="SCRAPE_WINDOW_SECONDS")

    # Closed sections are the interesting ones, so they get the short interval.
    poll_interval_closed_seconds: int = Field(default=60, alias="POLL_INTERVAL_CLOSED_SECONDS")
    poll_interval_open_seconds: int = Field(default=600, alias="POLL_INTERVAL_OPEN_SECONDS")

    # A section that keeps failing gets backed off rather than retried forever.
    max_consecutive_failures: int = Field(default=10, alias="MAX_CONSECUTIVE_FAILURES")

    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_api_base: str = Field(default="https://api.telegram.org", alias="TELEGRAM_API_BASE")

    smtp_host: str = Field(default="", alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_username: str = Field(default="", alias="SMTP_USERNAME")
    smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    smtp_from: str = Field(default="alerts@example.edu", alias="SMTP_FROM")
    smtp_use_tls: bool = Field(default=True, alias="SMTP_USE_TLS")

    api_key: str = Field(default="", alias="API_KEY")
    cors_origins: str = Field(default="", alias="CORS_ORIGINS")

    max_watches_per_destination: int = Field(default=25, alias="MAX_WATCHES_PER_DESTINATION")

    @field_validator("app_env")
    @classmethod
    def validate_app_env(cls, value: str) -> str:
        allowed = {"development", "test", "staging", "production"}
        normalized = value.lower().strip()
        if normalized not in allowed:
            raise ValueError(f"APP_ENV must be one of {sorted(allowed)}")
        return normalized

    @field_validator("scrape_requests_per_window", "scrape_window_seconds")
    @classmethod
    def must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be at least 1")
        return value

    @property
    def cors_origins_list(self) -> list[str]:
        if not self.cors_origins:
            return []
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host)

    def validate_production_safety(self) -> None:
        """
        Refuse to boot a production process that cannot do its one job.

        A service whose entire purpose is sending alerts, running with every
        notification channel disabled, is worse than one that failed to start:
        it looks healthy while quietly telling nobody anything.
        """
        if not self.is_production:
            return

        if not self.telegram_enabled and not self.email_enabled:
            raise ValueError(
                "No notification channel is configured. Set TELEGRAM_BOT_TOKEN "
                "or SMTP_HOST, or this service cannot deliver anything."
            )

        if not self.api_key:
            raise ValueError("API_KEY must be set in production to protect write endpoints.")

        if self.schedule_base_url.startswith("https://example.edu"):
            raise ValueError("SCHEDULE_BASE_URL still points at the example placeholder.")


@lru_cache
def get_settings() -> Settings:
    return Settings()
