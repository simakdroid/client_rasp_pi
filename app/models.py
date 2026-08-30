from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class Position(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    timestamp: datetime
    altitude_ft: int | None = None


class AircraftUpdate(BaseModel):
    icao: str = Field(min_length=6, max_length=6)
    callsign: str | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    altitude_ft: int | None = None
    speed_kt: float | None = None
    track_deg: float | None = Field(default=None, ge=0, lt=360)
    vertical_rate_fpm: int | None = None
    squawk: str | None = None
    category: str | None = None
    type_code: str | None = None
    type_desc: str | None = None
    on_ground: bool | None = None
    seen_s: float = 0
    received_at: datetime = Field(default_factory=utc_now)


@dataclass(slots=True)
class AircraftState:
    icao: str
    callsign: str | None = None
    lat: float | None = None
    lon: float | None = None
    altitude_ft: int | None = None
    speed_kt: float | None = None
    track_deg: float | None = None
    calculated_track_deg: float | None = None
    vertical_rate_fpm: int | None = None
    squawk: str | None = None
    category: str | None = None
    type_code: str | None = None
    type_desc: str | None = None
    on_ground: bool | None = None
    distance_km: float | None = None
    geofences: set[str] = field(default_factory=set)
    sector: str | None = None
    track: list[Position] = field(default_factory=list)
    updated_at: datetime = field(default_factory=utc_now)
    lost_at: datetime | None = None
    revision: int = 0

    def public_dict(self, include_track: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "icao": self.icao,
            "callsign": self.callsign,
            "lat": self.lat,
            "lon": self.lon,
            "altitude_ft": self.altitude_ft,
            "speed_kt": self.speed_kt,
            "track_deg": self.track_deg,
            "calculated_track_deg": self.calculated_track_deg,
            "vertical_rate_fpm": self.vertical_rate_fpm,
            "squawk": self.squawk,
            "category": self.category,
            "type_code": self.type_code,
            "type_desc": self.type_desc,
            "on_ground": self.on_ground,
            "distance_km": self.distance_km,
            "geofences": sorted(self.geofences),
            "sector": self.sector,
            "updated_at": self.updated_at.isoformat(),
            "lost_at": self.lost_at.isoformat() if self.lost_at else None,
            "status": "archived" if self.lost_at else "live",
            "revision": self.revision,
        }
        if include_track:
            result["track"] = [
                [point.lat, point.lon, point.altitude_ft, point.timestamp.isoformat()]
                for point in self.track
            ]
        return result
