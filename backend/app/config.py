"""Typed settings.

Two sources of truth, combined into one Settings object:

  * Environment (.env, container env) — runtime/infrastructure knobs
    (DATA_DIR, LOG_LEVEL, TZ, DEVICES_YAML_PATH).
  * devices.yaml — the RTU list, poll interval, and retention policy.
    Loaded lazily so tests can point `devices_yaml_path` at a fixture
    without touching the global Settings.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, Field, PositiveFloat, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# devices.yaml schema
# ---------------------------------------------------------------------------
class ChannelConfig(BaseModel):
    """One analog input channel on an AIME 8U device."""

    channel: Annotated[int, Field(ge=1, le=8)]
    name: str
    unit: str | None = None


class DeviceConfig(BaseModel):
    """One AIME 8U RTU."""

    id: PositiveInt
    name: str
    host: str
    port: int = 502
    unit_id: Annotated[int, Field(ge=1, le=247)] = 1
    location: str | None = None
    enabled: bool = True
    channels: list[ChannelConfig]


class RetentionConfig(BaseModel):
    raw_days: PositiveInt = 7
    one_min_days: PositiveInt = 90
    one_hour_years: PositiveInt = 2


class DevicesFile(BaseModel):
    """Top-level shape of devices.yaml."""

    poll_interval_s: PositiveFloat = 1.0
    retention: RetentionConfig = RetentionConfig()
    devices: list[DeviceConfig]


# ---------------------------------------------------------------------------
# Process-level settings from .env / environment
# ---------------------------------------------------------------------------
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    data_dir: Path = Path("/data")
    log_level: str = "INFO"
    tz: str = "Asia/Kolkata"
    devices_yaml_path: Path = Path("/app/devices.yaml")

    def load_devices_file(self) -> DevicesFile:
        """Parse and validate devices.yaml into typed models."""
        with self.devices_yaml_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        return DevicesFile.model_validate(raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor; tests can call `get_settings.cache_clear()`."""
    return Settings()
