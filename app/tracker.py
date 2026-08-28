from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from pyproj import Geod

from .gis import LayerManager
from .models import AircraftState, AircraftUpdate, Position

GEOD = Geod(ellps="WGS84")


class AircraftTracker:
    def __init__(
        self,
        station_lat: float,
        station_lon: float,
        layer_manager: LayerManager,
        ttl_s: int,
        max_track_points: int,
        min_track_distance_m: float,
    ) -> None:
        self.station_lat = station_lat
        self.station_lon = station_lon
        self.layer_manager = layer_manager
        self.ttl = timedelta(seconds=ttl_s)
        self.max_track_points = max_track_points
        self.min_track_distance_m = min_track_distance_m
        self._aircraft: dict[str, AircraftState] = {}
        self._changed: set[str] = set()
        self._removed: set[str] = set()
        self._track_appends: dict[str, list[Position]] = {}
        self._lock = asyncio.Lock()

    async def apply(self, updates: list[AircraftUpdate]) -> None:
        async with self._lock:
            for update in updates:
                self._merge(update)

    async def prune(self) -> None:
        threshold = datetime.now(UTC) - self.ttl
        async with self._lock:
            expired = [
                icao for icao, state in self._aircraft.items() if state.updated_at < threshold
            ]
            for icao in expired:
                del self._aircraft[icao]
                self._changed.discard(icao)
                self._track_appends.pop(icao, None)
                self._removed.add(icao)

    async def snapshot(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [
                state.public_dict(include_track=True)
                for state in self._aircraft.values()
            ]

    async def consume_delta(self) -> dict[str, Any] | None:
        async with self._lock:
            if not self._changed and not self._removed:
                return None
            changed = []
            for icao in self._changed:
                if icao not in self._aircraft:
                    continue
                item = self._aircraft[icao].public_dict(include_track=False)
                if points := self._track_appends.get(icao):
                    item["track_append"] = [
                        [point.lat, point.lon, point.altitude_ft, point.timestamp.isoformat()]
                        for point in points
                    ]
                changed.append(item)
            result = {
                "type": "delta",
                "timestamp": datetime.now(UTC).isoformat(),
                "upsert": changed,
                "remove": sorted(self._removed),
            }
            self._changed.clear()
            self._removed.clear()
            self._track_appends.clear()
            return result

    def _merge(self, update: AircraftUpdate) -> None:
        state = self._aircraft.get(update.icao)
        if state is None:
            state = AircraftState(icao=update.icao, updated_at=update.received_at)
            self._aircraft[update.icao] = state

        changed = False
        for name in (
            "callsign",
            "lat",
            "lon",
            "altitude_ft",
            "speed_kt",
            "track_deg",
            "vertical_rate_fpm",
            "squawk",
            "category",
            "on_ground",
        ):
            value = getattr(update, name)
            if value is not None and value != getattr(state, name):
                setattr(state, name, value)
                changed = True

        state.updated_at = max(state.updated_at, update.received_at)
        if update.lat is not None and update.lon is not None:
            previous_track_time = state.track[-1].timestamp if state.track else None
            changed |= self._update_position(state, update)
            if state.track and state.track[-1].timestamp != previous_track_time:
                self._track_appends.setdefault(state.icao, []).append(state.track[-1])

        if changed:
            state.revision += 1
            self._changed.add(state.icao)
            self._removed.discard(state.icao)

    def _update_position(self, state: AircraftState, update: AircraftUpdate) -> bool:
        assert update.lat is not None and update.lon is not None
        changed = False
        _, _, distance_m = GEOD.inv(
            self.station_lon, self.station_lat, update.lon, update.lat
        )
        distance_km = round(distance_m / 1000, 2)
        if state.distance_km != distance_km:
            state.distance_km = distance_km
            changed = True

        geofences = self.layer_manager.matching_geofences(
            update.lon, update.lat, update.altitude_ft
        )
        if geofences != state.geofences:
            state.geofences = geofences
            changed = True

        point = Position(
            lat=update.lat,
            lon=update.lon,
            altitude_ft=update.altitude_ft,
            timestamp=update.received_at,
        )
        if not state.track:
            state.track.append(point)
            return True

        previous = state.track[-1]
        if point.timestamp <= previous.timestamp:
            return changed
        forward_azimuth, _, segment_m = GEOD.inv(
            previous.lon, previous.lat, point.lon, point.lat
        )
        if segment_m < self.min_track_distance_m:
            return changed

        state.calculated_track_deg = round(forward_azimuth % 360, 1)
        elapsed_minutes = (point.timestamp - previous.timestamp).total_seconds() / 60
        if (
            update.vertical_rate_fpm is None
            and elapsed_minutes > 0
            and point.altitude_ft is not None
            and previous.altitude_ft is not None
        ):
            state.vertical_rate_fpm = round(
                (point.altitude_ft - previous.altitude_ft) / elapsed_minutes
            )
        state.track.append(point)
        if len(state.track) > self.max_track_points:
            del state.track[: len(state.track) - self.max_track_points]
        return True
