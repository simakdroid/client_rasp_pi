from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, BaseModel, Field, HttpUrl, field_validator
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
        populate_by_name=True,
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
    sbs_timezone: str = "UTC"

    @field_validator("sbs_timezone")
    @classmethod
    def _sbs_timezone(cls, value: str) -> str:
        name = value.strip() or "UTC"
        try:
            ZoneInfo(name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone {name}") from exc
        return name

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

    max_active_aircraft: int = Field(default=500, ge=1, le=20000)
    websocket_interval_s: float = Field(default=0.75, ge=0.2, le=5)
    websocket_heartbeat_s: float = Field(default=10, ge=1, le=60)
    websocket_queue_size: int = Field(default=32, ge=1, le=1000)
    websocket_max_clients: int = Field(default=32, ge=1, le=500)
    websocket_send_timeout_s: float = Field(default=10, ge=1, le=120)
    admin_token: str = ""
    trusted_hosts: list[str] = []
    layers_dir: Path = Path(__file__).resolve().parent.parent / "data" / "layers"
    static_dir: Path = Path(__file__).resolve().parent / "static"
    cors_origins: list[str] = []

    osm_url: str = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
    ofm_url: str | None = None

    @field_validator("ofm_url", mode="before")
    @classmethod
    def blank_ofm_url_to_none(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    radio_channels_json: str = "[]"
    radio_stats_path: Path | None = Path("/run/rtl-airband/stats.prom")
    radio_auto_detect: bool = True
    radio_min_rtl_receivers: int = Field(default=2, ge=1, le=16)
    radio_receiver_serial: str = Field(
        default="0118",
        validation_alias=AliasChoices(
            "AIRMON_RADIO_RECEIVER_SERIAL",
            "RADIO_RECEIVER_SERIAL",
            "VHF_SERIAL",
        ),
    )
    adsb_preferred_serial: str = Field(
        default="1090",
        validation_alias=AliasChoices(
            "AIRMON_ADSB_PREFERRED_SERIAL",
            "ADSB_PREFERRED_SERIAL",
        ),
    )
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
