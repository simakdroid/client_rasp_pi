from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pyproj import Geod

from .persist import atomic_write_json, read_json_file

LOGGER = logging.getLogger(__name__)
GEOD = Geod(ellps="WGS84")
BINS = 360
MIN_RANGE_M = 200.0
MAX_COVERAGE_BYTES = 1_000_000
HOUR_WINDOW = 24
ALTITUDE_BANDS = (
    ("0-5k", "0–5000 ft", 0, 5000),
    ("5-15k", "5000–15000 ft", 5000, 15000),
    ("15-25k", "15000–25000 ft", 15000, 25000),
    ("25k+", "25000+ ft", 25000, None),
)


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
        self._observations = 0
        self._revision = 0
        self._dirty = False
        self._started_at = datetime.now(UTC)
        self._updated_at: datetime | None = None
        self._heard_at: datetime | None = None
        self._save_error: str | None = None
        self._load_error: str | None = None
        self._last_pos: dict[str, tuple[float, float]] = {}
        self._bands = _empty_bands()
        self._hourly: dict[str, dict[str, float | int]] = {}
        self.load()

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def revision(self) -> int:
        return self._revision

    def observe(
        self,
        azimuth_deg: float,
        distance_m: float,
        *,
        icao: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        altitude_ft: int | None = None,
    ) -> bool:
        if icao and lat is not None and lon is not None:
            key = (round(float(lat), 5), round(float(lon), 5))
            if self._last_pos.get(icao) == key:
                return False
            self._last_pos[icao] = key
            if len(self._last_pos) > 2000:
                extra = list(self._last_pos)[:500]
                for old in extra:
                    self._last_pos.pop(old, None)
        now = datetime.now(UTC)
        self._observations += 1
        self._heard_at = now
        self._record_band_and_hour(now, distance_m, altitude_ft)
        self._dirty = True
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
        self._updated_at = now
        return True

    def forget(self, icao: str) -> None:
        self._last_pos.pop(icao, None)

    def snapshot(self) -> dict[str, Any]:
        filled = sum(1 for range_m in self._range_m if range_m > 0)
        max_range_m = max(self._range_m) if filled else 0.0
        return {
            "revision": self._revision,
            "bin_deg": 1,
            "kind": "historical_max_range",
            "caption": "Исторический максимум дальности, не гарантированная зона приёма",
            "samples": self._samples,
            "range_updates": self._samples,
            "observations": self._observations,
            "filled_bins": filled,
            "max_range_km": round(max_range_m / 1000, 2),
            "altitude_bands": _band_snapshot(self._bands),
            "hourly": _hourly_snapshot(self._hourly),
            "started_at": self._started_at.isoformat(),
            "updated_at": self._updated_at.isoformat() if self._updated_at else None,
            "heard_at": self._heard_at.isoformat() if self._heard_at else None,
            "points": self._polygon() if filled else [],
            "saved": (not self._dirty) or self.path is None,
            "save_error": self._save_error,
            "load_error": self._load_error,
        }

    def reset(self) -> None:
        self._range_m = [0.0] * BINS
        self._hits = [0] * BINS
        self._samples = 0
        self._observations = 0
        self._last_pos.clear()
        self._bands = _empty_bands()
        self._hourly.clear()
        self._revision += 1
        self._dirty = True
        self._started_at = datetime.now(UTC)
        self._updated_at = None
        self._heard_at = None

    def load(self) -> None:
        self._load_error = None
        if self.path is None or not self.path.is_file():
            return
        try:
            payload = read_json_file(self.path, max_bytes=MAX_COVERAGE_BYTES)
            parsed = self._parse_payload(payload)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._quarantine(str(exc))
            return
        if parsed is None:
            LOGGER.info("Coverage rose station moved; starting a new accumulation")
            return
        (
            self._range_m,
            self._hits,
            self._samples,
            self._observations,
            self._started_at,
            self._updated_at,
            self._heard_at,
            self._revision,
        ) = parsed
        self._bands = _parse_bands(payload, self.max_range_m)
        self._hourly = _parse_hourly(payload, self.max_range_m)
        self._dirty = False

    def dump(self) -> dict[str, Any]:
        return {
            "station_lat": self.station_lat,
            "station_lon": self.station_lon,
            "started_at": self._started_at.isoformat(),
            "updated_at": self._updated_at.isoformat() if self._updated_at else None,
            "samples": self._samples,
            "range_updates": self._samples,
            "observations": self._observations,
            "revision": self._revision,
            "range_m": [round(value, 1) for value in self._range_m],
            "hits": list(self._hits),
            "heard_at": self._heard_at.isoformat() if self._heard_at else None,
            "altitude_bands": {
                key: {
                    "observations": int(item["observations"]),
                    "max_range_m": round(float(item["max_range_m"]), 1),
                }
                for key, item in self._bands.items()
            },
            "hourly": [
                {
                    "hour": hour,
                    "observations": int(item["observations"]),
                    "max_range_m": round(float(item["max_range_m"]), 1),
                }
                for hour, item in self._hourly.items()
            ],
        }

    def flush(self) -> bool:
        if self.path is None:
            self._dirty = False
            self._save_error = None
            return True
        if not self._dirty:
            return True
        try:
            atomic_write_json(self.path, self.dump())
        except OSError as exc:
            self._save_error = str(exc)
            LOGGER.warning("Cannot write coverage rose %s: %s", self.path, exc)
            return False
        self._dirty = False
        self._save_error = None
        return True

    def mark_saved(self, revision: int) -> None:
        if self._revision == revision:
            self._dirty = False
            self._save_error = None
        else:
            self._dirty = True

    def mark_save_error(self, message: str) -> None:
        self._save_error = message

    def _parse_payload(
        self, payload: object
    ) -> (
        tuple[
            list[float],
            list[int],
            int,
            int,
            datetime,
            datetime | None,
            datetime | None,
            int,
        ]
        | None
    ):
        if not isinstance(payload, dict):
            raise ValueError("coverage file root must be an object")
        if not self._same_station(payload):
            return None
        ranges = payload.get("range_m")
        if not isinstance(ranges, list) or len(ranges) != BINS:
            raise ValueError("range_m must contain 360 finite ranges")
        range_m = [_bounded_range(value, self.max_range_m) for value in ranges]
        hits_raw = payload.get("hits")
        if hits_raw is None:
            hits = [1 if value else 0 for value in range_m]
        elif not isinstance(hits_raw, list) or len(hits_raw) != BINS:
            raise ValueError("hits must contain 360 counts")
        else:
            hits = [_count(value) for value in hits_raw]
        if payload.get("range_updates") is not None:
            samples = _count(payload["range_updates"])
        elif "samples" in payload and payload["samples"] is not None:
            samples = _count(payload["samples"])
        else:
            samples = sum(hits)
        if payload.get("observations") is not None:
            observations = _count(payload["observations"])
        else:
            observations = samples
        revision = payload.get("revision", 0)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("revision must be a non-negative integer")
        started = _timestamp(payload.get("started_at")) or datetime.now(UTC)
        updated = _timestamp(payload.get("updated_at"))
        heard = _timestamp(payload.get("heard_at")) or updated
        bands_raw = payload.get("altitude_bands")
        if bands_raw is not None and not isinstance(bands_raw, dict):
            raise ValueError("altitude_bands must be an object")
        hourly_raw = payload.get("hourly")
        if hourly_raw is not None and not isinstance(hourly_raw, list):
            raise ValueError("hourly must be an array")
        return range_m, hits, samples, observations, started, updated, heard, revision

    def _quarantine(self, reason: str) -> None:
        self._load_error = reason
        LOGGER.warning("Coverage rose %s ignored: %s", self.path, reason)
        if self.path is None or not self.path.is_file():
            return
        bad = self.path.with_name(f"{self.path.name}.bad")
        try:
            self.path.replace(bad)
            LOGGER.warning("Moved unreadable coverage file to %s", bad)
        except OSError as exc:
            LOGGER.warning("Cannot quarantine %s: %s", self.path, exc)

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

    def _record_band_and_hour(
        self,
        now: datetime,
        distance_m: float,
        altitude_ft: int | None,
    ) -> None:
        in_range = MIN_RANGE_M <= distance_m <= self.max_range_m
        band_id = _band_id(altitude_ft)
        if band_id is not None:
            band = self._bands[band_id]
            band["observations"] = int(band["observations"]) + 1
            if in_range:
                band["max_range_m"] = max(float(band["max_range_m"]), distance_m)
        hour_key = _hour_key(now)
        bucket = self._hourly.setdefault(hour_key, {"observations": 0, "max_range_m": 0.0})
        bucket["observations"] = int(bucket["observations"]) + 1
        if in_range:
            bucket["max_range_m"] = max(float(bucket["max_range_m"]), distance_m)
        cutoff = now - timedelta(hours=HOUR_WINDOW)
        for key in list(self._hourly):
            stamp = _timestamp(key)
            if stamp is None or stamp < cutoff:
                self._hourly.pop(key, None)


def _bounded_range(value: object, cap_m: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("range_m values must be finite numbers")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("range_m values must be finite numbers")
    if number <= 0:
        return 0.0
    return min(number, cap_m)


def _count(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("count values must be integers") from None
    if isinstance(value, bool) or number < 0:
        raise ValueError("count values must be non-negative integers")
    return number


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


def _empty_bands() -> dict[str, dict[str, float | int]]:
    return {
        key: {"observations": 0, "max_range_m": 0.0}
        for key, _label, _lo, _hi in ALTITUDE_BANDS
    }


def _band_id(altitude_ft: int | None) -> str | None:
    if altitude_ft is None:
        return None
    for key, _label, low, high in ALTITUDE_BANDS:
        if altitude_ft < low:
            continue
        if high is None or altitude_ft < high:
            return key
    return None


def _band_snapshot(bands: dict[str, dict[str, float | int]]) -> list[dict[str, Any]]:
    result = []
    for key, label, _low, _high in ALTITUDE_BANDS:
        item = bands.get(key) or {"observations": 0, "max_range_m": 0.0}
        max_range_m = float(item["max_range_m"])
        result.append(
            {
                "id": key,
                "label": label,
                "observations": int(item["observations"]),
                "max_range_km": round(max_range_m / 1000, 2) if max_range_m else 0.0,
            }
        )
    return result


def _hourly_snapshot(hourly: dict[str, dict[str, float | int]]) -> list[dict[str, Any]]:
    rows = []
    for hour in sorted(hourly):
        item = hourly[hour]
        max_range_m = float(item["max_range_m"])
        rows.append(
            {
                "hour": hour,
                "observations": int(item["observations"]),
                "max_range_km": round(max_range_m / 1000, 2) if max_range_m else 0.0,
            }
        )
    return rows[-HOUR_WINDOW:]


def _hour_key(now: datetime) -> str:
    return now.replace(minute=0, second=0, microsecond=0).isoformat()


def _parse_bands(
    payload: dict[str, Any], cap_m: float
) -> dict[str, dict[str, float | int]]:
    bands = _empty_bands()
    raw = payload.get("altitude_bands")
    if not isinstance(raw, dict):
        return bands
    for key in bands:
        item = raw.get(key)
        if not isinstance(item, dict):
            continue
        try:
            bands[key] = {
                "observations": _count(item.get("observations", 0)),
                "max_range_m": _bounded_range(item.get("max_range_m", 0), cap_m),
            }
        except ValueError:
            continue
    return bands


def _parse_hourly(
    payload: dict[str, Any], cap_m: float
) -> dict[str, dict[str, float | int]]:
    raw = payload.get("hourly")
    if not isinstance(raw, list):
        return {}
    result: dict[str, dict[str, float | int]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        hour = item.get("hour")
        stamp = _timestamp(hour) if isinstance(hour, str) else None
        if stamp is None:
            continue
        try:
            result[stamp.replace(minute=0, second=0, microsecond=0).isoformat()] = {
                "observations": _count(item.get("observations", 0)),
                "max_range_m": _bounded_range(item.get("max_range_m", 0), cap_m),
            }
        except ValueError:
            continue
    return result
