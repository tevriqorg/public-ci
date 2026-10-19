from functools import lru_cache
from datetime import date
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import SecretStr


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HOME_VISION_", extra="ignore")

    environment: str = "development"
    database_url: str = "sqlite:///./home-vision.db"
    data_root: Path = Path("./data")
    nvr_base_url: str | None = None
    nvr_username: str | None = None
    nvr_password: SecretStr | None = None
    nvr_id: str | None = None
    nvr_tracks: str | None = None
    nvr_scan_min_date: date | None = None
    # Unattended archive discovery/enqueue is opt-in and fails closed until
    # the configured NVR business timezone has been verified read-only.
    archive_auto_enqueue: bool = False
    nvr_timezone_verified: bool = False
    nvr_scan_initial_days: int = 60
    archive_queue_enabled: bool = False
    archive_heartbeat_path: Path = Path("/state/archive-worker-heartbeat.json")
    archive_download_method: str = "POST"
    archive_poll_seconds: int = 2
    thumbnail_timezone: str = "Asia/Shanghai"
    thumbnail_schedule_hour: int = 12
    thumbnail_schedule_minute: int = 0
    thumbnail_poll_seconds: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
