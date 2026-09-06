from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class RadioChannel(BaseModel):
    id: str
    name: str
    frequency_mhz: float = Field(ge=118.0, le=137.0)
    stream_url: str
    status_url: str | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="AIRMON_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    app_name: str = "Raspberry Pi Air Monitor"
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)

    station_lat: float = Field(default=57.1896, ge=-90, le=90)
    station_lon: float = Field(default=65.3243, ge=-180, le=180)
    station_name: str = "Тюмень/Рощино"

    adsb_source: Literal["json", "sbs"] = "json"
    readsb_json_path: Path = Path("/run/readsb/aircraft.json")
    sbs_host: str = "127.0.0.1"
    sbs_port: int = Field(default=30003, ge=1, le=65535)
    raw_host: str = "127.0.0.1"
    raw_port: int = Field(default=30002, ge=1, le=65535)
    raw_log_size: int = Field(default=1000, ge=10, le=50000)
    adsb_poll_interval_s: float = Field(default=0.75, ge=0.2, le=10)
    aircraft_ttl_s: int = Field(default=60, ge=5, le=3600)
    track_max_points: int = Field(default=300, ge=2, le=5000)
    track_min_distance_m: float = Field(default=30, ge=0, le=10000)
    event_log_size: int = Field(default=500, ge=10, le=10000)
    archive_max_aircraft: int = Field(default=100, ge=0, le=1000)
    coverage_path: Path = Path(__file__).resolve().parent.parent / "data" / "coverage-rose.json"
    coverage_max_km: float = Field(default=450, ge=10, le=2000)
    aircraft_types_path: Path = (
        Path(__file__).resolve().parent.parent / "data" / "aircraft-types.json"
    )
    sessions_dir: Path = Path(__file__).resolve().parent.parent / "data" / "sessions"
    sessions_keep_days: int = Field(default=7, ge=1, le=90)

    websocket_interval_s: float = Field(default=0.75, ge=0.2, le=5)
    layers_dir: Path = Path(__file__).resolve().parent.parent / "data" / "layers"
    static_dir: Path = Path(__file__).resolve().parent / "static"
    cors_origins: list[str] = []

    osm_url: str = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
    ofm_url: str | None = None
    radio_channels_json: str = "[]"
    radio_stats_path: Path | None = Path("/run/rtl-airband/stats.prom")
    radio_auto_detect: bool = True
    radio_min_rtl_receivers: int = Field(default=2, ge=1, le=16)
    radio_receiver_serial: str = "0118"
    usb_sysfs_path: Path = Path("/sys/bus/usb/devices")
    icecast_status_url: HttpUrl | None = None

    @property
    def radio_channels(self) -> list[RadioChannel]:
        return [RadioChannel.model_validate(item) for item in self._radio_json()]

    def _radio_json(self) -> list[dict[str, object]]:
        import json

        value = json.loads(self.radio_channels_json)
        if not isinstance(value, list):
            raise ValueError("AIRMON_RADIO_CHANNELS_JSON must contain a JSON array")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
