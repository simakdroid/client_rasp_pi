from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator

ICAO_RE = re.compile(r"^[0-9a-f]{6}$")
MAX_TEXT = 80


def utc_now() -> datetime:
    return datetime.now(UTC)


def normalize_icao_hex(value: object) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("~"):
        raise ValueError("non-ICAO addresses are not merged with ICAO identities")
    if not ICAO_RE.fullmatch(text):
        raise ValueError("ICAO must be a 24-bit hex address")
    return text


class Position(BaseModel):
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    timestamp: AwareDatetime
    altitude_ft: int | None = None


class AircraftTypeInput(BaseModel):
    icao: str
    type_code: str = Field(max_length=16)
    type_desc: str | None = Field(default=None, max_length=MAX_TEXT)

    @field_validator("icao", mode="before")
    @classmethod
    def _icao(cls, value: object) -> str:
        return normalize_icao_hex(value)


class AircraftUpdate(BaseModel):
    icao: str
    callsign: str | None = Field(default=None, max_length=16)
    lat: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    lon: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    altitude_ft: int | None = None
    altitude_reference: str | None = Field(default=None, max_length=8)
    speed_kt: float | None = Field(default=None, allow_inf_nan=False)
    track_deg: float | None = Field(default=None, ge=0, lt=360, allow_inf_nan=False)
    true_heading_deg: float | None = Field(default=None, ge=0, lt=360, allow_inf_nan=False)
    vertical_rate_fpm: int | None = None
    vertical_rate_source: str | None = Field(default=None, max_length=8)
    squawk: str | None = Field(default=None, max_length=8)
    category: str | None = Field(default=None, max_length=8)
    type_code: str | None = Field(default=None, max_length=16)
    type_desc: str | None = Field(default=None, max_length=MAX_TEXT)
    on_ground: bool | None = None
    seen_s: float = Field(default=0, ge=0, allow_inf_nan=False)
    received_at: AwareDatetime = Field(default_factory=utc_now)
    position_at: AwareDatetime | None = None

    @field_validator("icao", mode="before")
    @classmethod
    def _icao(cls, value: object) -> str:
        return normalize_icao_hex(value)

    @field_validator("altitude_reference")
    @classmethod
    def _altitude_reference(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        text = str(value).strip().lower()
        if text not in {"baro", "geom"}:
            raise ValueError("altitude_reference must be baro or geom")
        return text

    @field_validator("vertical_rate_source")
    @classmethod
    def _vertical_rate_source(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        text = str(value).strip().lower()
        if text not in {"baro", "geom", "derived"}:
            raise ValueError("vertical_rate_source must be baro, geom or derived")
        return text

    @model_validator(mode="after")
    def _paired_coordinates(self) -> AircraftUpdate:
        if (self.lat is None) != (self.lon is None):
            self.lat = None
            self.lon = None
            self.position_at = None
        return self


@dataclass(slots=True)
class AircraftState:
    icao: str
    callsign: str | None = None
    lat: float | None = None
    lon: float | None = None
    altitude_ft: int | None = None
    altitude_reference: str | None = None
    speed_kt: float | None = None
    track_deg: float | None = None
    true_heading_deg: float | None = None
    calculated_track_deg: float | None = None
    vertical_rate_fpm: int | None = None
    vertical_rate_source: str | None = None
    squawk: str | None = None
    category: str | None = None
    type_code: str | None = None
    type_desc: str | None = None
    on_ground: bool | None = None
    distance_km: float | None = None
    azimuth_deg: float | None = None
    geofences: set[str] = field(default_factory=set)
    geofence_ids: set[str] = field(default_factory=set)
    sector: str | None = None
    track: list[Position] = field(default_factory=list)
    updated_at: datetime = field(default_factory=utc_now)
    last_message_at: datetime | None = None
    position_at: datetime | None = None
    altitude_at: datetime | None = None
    velocity_at: datetime | None = None
    identity_at: datetime | None = None
    started_at: datetime | None = None
    lost_at: datetime | None = None
    contact_id: str | None = None
    revision: int = 0

    def public_dict(self, include_track: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "icao": self.icao,
            "callsign": self.callsign,
            "lat": self.lat,
            "lon": self.lon,
            "altitude_ft": self.altitude_ft,
            "altitude_reference": self.altitude_reference,
            "speed_kt": self.speed_kt,
            "track_deg": self.track_deg,
            "true_heading_deg": self.true_heading_deg,
            "calculated_track_deg": self.calculated_track_deg,
            "vertical_rate_fpm": self.vertical_rate_fpm,
            "vertical_rate_source": self.vertical_rate_source,
            "squawk": self.squawk,
            "category": self.category,
            "type_code": self.type_code,
            "type_desc": self.type_desc,
            "on_ground": self.on_ground,
            "distance_km": self.distance_km,
            "azimuth_deg": self.azimuth_deg,
            "geofences": sorted(self.geofences),
            "geofence_ids": sorted(self.geofence_ids),
            "sector": self.sector,
            "updated_at": self.updated_at.isoformat(),
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
            "position_at": self.position_at.isoformat() if self.position_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "lost_at": self.lost_at.isoformat() if self.lost_at else None,
            "contact_id": self.contact_id,
            "status": "archived" if self.lost_at else "live",
            "revision": self.revision,
        }
        if include_track:
            result["track"] = [
                [point.lat, point.lon, point.altitude_ft, point.timestamp.isoformat()]
                for point in self.track
            ]
        return result
