from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pyproj import Geod

LOGGER = logging.getLogger(__name__)
GEOD = Geod(ellps="WGS84")
BINS = 360
MIN_RANGE_M = 200.0


class CoverageRose:
    """Accumulates the farthest heard aircraft in each 1° azimuth bin."""

    def __init__(
        self,
        station_lat: float,
        station_lon: float,
        path: Path | None = None,
        max_range_km: float = 450,
    ) -> None:
        self.station_lat = station_lat
        self.station_lon = station_lon
        self.path = path
        self.max_range_m = max_range_km * 1000
        self._range_m = [0.0] * BINS
        self._hits = [0] * BINS
        self._samples = 0
        self._revision = 0
        self._dirty = False
        self._started_at = datetime.now(UTC)
        self._updated_at: datetime | None = None
        self.load()

    def observe(self, azimuth_deg: float, distance_m: float) -> bool:
        if distance_m < MIN_RANGE_M or distance_m > self.max_range_m:
            return False
        bin_index = int(azimuth_deg % 360) % BINS
        grew = distance_m > self._range_m[bin_index]
        if not grew and self._hits[bin_index]:
            return False
        if grew:
            self._range_m[bin_index] = distance_m
        self._hits[bin_index] += 1
        self._samples += 1
        self._revision += 1
        self._dirty = True
        self._updated_at = datetime.now(UTC)
        return True

    def snapshot(self) -> dict[str, Any]:
        filled = sum(1 for range_m in self._range_m if range_m > 0)
        max_range_m = max(self._range_m) if filled else 0.0
        return {
            "revision": self._revision,
            "bin_deg": 1,
            "samples": self._samples,
            "filled_bins": filled,
            "max_range_km": round(max_range_m / 1000, 2),
            "started_at": self._started_at.isoformat(),
            "updated_at": self._updated_at.isoformat() if self._updated_at else None,
            "points": self._polygon() if filled else [],
        }

    def reset(self) -> None:
        self._range_m = [0.0] * BINS
        self._hits = [0] * BINS
        self._samples = 0
        self._revision += 1
        self._dirty = True
        self._started_at = datetime.now(UTC)
        self._updated_at = None

    def load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Cannot read coverage rose %s: %s", self.path, exc)
            return
        if not self._same_station(payload):
            LOGGER.info("Coverage rose station moved; starting a new accumulation")
            return
        ranges = payload.get("range_m")
        hits = payload.get("hits")
        if not isinstance(ranges, list) or len(ranges) != BINS:
            return
        self._range_m = [_positive(value) for value in ranges]
        self._hits = (
            [_count(value) for value in hits]
            if isinstance(hits, list) and len(hits) == BINS
            else [1 if range_m else 0 for range_m in self._range_m]
        )
        self._samples = int(payload.get("samples") or sum(self._hits))
        self._started_at = _timestamp(payload.get("started_at")) or datetime.now(UTC)
        self._updated_at = _timestamp(payload.get("updated_at"))
        self._revision = int(payload.get("revision") or 0)
        self._dirty = False

    def dump(self) -> dict[str, Any]:
        return {
            "station_lat": self.station_lat,
            "station_lon": self.station_lon,
            "started_at": self._started_at.isoformat(),
            "updated_at": self._updated_at.isoformat() if self._updated_at else None,
            "samples": self._samples,
            "revision": self._revision,
            "range_m": [round(value, 1) for value in self._range_m],
            "hits": list(self._hits),
        }

    def flush(self) -> None:
        if not self._dirty or self.path is None:
            return
        payload = self.dump()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f"{self.path.name}.tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(self.path)
            self._dirty = False
        except OSError as exc:
            LOGGER.warning("Cannot write coverage rose %s: %s", self.path, exc)

    def _polygon(self) -> list[list[float]]:
        points: list[list[float]] = []
        for azimuth, range_m in enumerate(self._range_m):
            if range_m <= 0:
                points.append([self.station_lat, self.station_lon])
                continue
            lon, lat, _ = GEOD.fwd(
                self.station_lon, self.station_lat, float(azimuth), range_m
            )
            points.append([round(lat, 5), round(lon, 5)])
        points.append(points[0])
        return points

    def _same_station(self, payload: dict[str, Any]) -> bool:
        lat = payload.get("station_lat")
        lon = payload.get("station_lon")
        try:
            _, _, distance_m = GEOD.inv(
                self.station_lon, self.station_lat, float(lon), float(lat)
            )
        except (TypeError, ValueError):
            return False
        return distance_m < 50


def _positive(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def _count(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
