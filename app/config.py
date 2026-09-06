from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_name: str = "Threads Public Monitor"
    app_env: str = "development"
    database_url: str = "sqlite:///./data/threads-monitor.db"
    media_root: Path = Path("./data/media")
    browser_profile_dir: Path = Path("./browser-profile")
    tailscale_ip: str = "100.120.200.116"
    web_port: int = 8080
    login_port: int = 6080
    timezone: str = "Asia/Taipei"

    max_active_accounts: int = 16
    max_media_bytes: int = 100 * 1024**3
    media_warn_percent: int = 80
    media_stop_percent: int = 95
    max_media_file_bytes: int = 500 * 1024**2

    daily_batch_limit: int = 200
    batch_min_delay_seconds: int = 180
    batch_max_delay_seconds: int = 480
    schedule_jitter_minutes: int = 30
    batch_size: int = 10
    backfill_limit: int = 100
    relationship_batch_size: int = 25
    relationship_max_attempts: int = Field(default=3, ge=1)
    relationship_retry_min_delay_seconds: int = Field(default=2700, ge=0)
    relationship_retry_max_delay_seconds: int = Field(default=5400, ge=0)
    rate_limit_initial_min_delay_seconds: int = Field(default=2700, ge=1)
    rate_limit_initial_max_delay_seconds: int = Field(default=5400, ge=1)
    rate_limit_backoff_multiplier: int = Field(default=4, ge=1)
    rate_limit_max_delay_seconds: int = Field(default=86400, ge=1)
    rate_limit_streak_reset_seconds: int = Field(default=86400, ge=1)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_notification_timeout_seconds: int = Field(default=15, ge=1, le=60)
    telegram_notification_max_attempts: int = Field(default=5, ge=1, le=20)
    log_level: str = "INFO"

    chromium_executable: str = Field(default="/usr/bin/chromium")

    @model_validator(mode="after")
    def validate_relationship_retry_delay(self) -> Settings:
        if (
            self.relationship_retry_min_delay_seconds
            > self.relationship_retry_max_delay_seconds
        ):
            raise ValueError(
                "relationship retry minimum delay must not exceed maximum delay"
            )
        return self

    @model_validator(mode="after")
    def validate_rate_limit_delay(self) -> Settings:
        if (
            self.rate_limit_initial_min_delay_seconds
            > self.rate_limit_initial_max_delay_seconds
        ):
            raise ValueError(
                "rate limit initial minimum delay must not exceed initial maximum delay"
            )
        if self.rate_limit_initial_max_delay_seconds > self.rate_limit_max_delay_seconds:
            raise ValueError(
                "rate limit initial maximum delay must not exceed maximum delay"
            )
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def telegram_notifications_enabled(self) -> bool:
        return bool(self.telegram_bot_token.strip() and self.telegram_chat_id.strip())

    @property
    def effective_relationship_batch_size(self) -> int:
        """Clamp checkpoints to a range that limits both rescans and long bursts."""
        return min(50, max(25, self.relationship_batch_size))

    def relationship_batch_size_for(self, expected_count: int | None) -> int:
        """Use larger checkpoints for long lists to reduce repeated top rescans."""
        if expected_count is not None and expected_count >= 200:
            return 50
        return self.effective_relationship_batch_size

    def ensure_directories(self) -> None:
        self.media_root.mkdir(parents=True, exist_ok=True)
        self.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        if self.database_url.startswith("sqlite:///"):
            raw = self.database_url.removeprefix("sqlite:///")
            if raw and raw != ":memory:":
                Path(raw).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
