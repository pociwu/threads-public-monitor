from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field
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
    relationship_batch_size: int = 5
    log_level: str = "INFO"

    chromium_executable: str = Field(default="/usr/bin/chromium")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

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
