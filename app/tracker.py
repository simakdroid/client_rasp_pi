from __future__ import annotations

import asyncio
from collections import deque
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
        max_events: int = 500,
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
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._event_sequence = 0
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
                state = self._aircraft.pop(icao)
                self._changed.discard(icao)
                self._track_appends.pop(icao, None)
                self._removed.add(icao)
                self._append_event(state, "lost", datetime.now(UTC))

    async def snapshot(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [
                state.public_dict(include_track=True)
                for state in self._aircraft.values()
            ]

    async def recent_events(
        self, after_id: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        async with self._lock:
            events = [event for event in self._events if event["id"] > after_id]
            return {
                "events": events[-limit:],
                "last_id": self._event_sequence,
            }

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
        is_new = state is None
        if is_new:
            state = AircraftState(icao=update.icao, updated_at=update.received_at)
            self._aircraft[update.icao] = state

        changed = is_new
        position_added = False
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
                position_added = True

        if changed:
            state.revision += 1
            self._changed.add(state.icao)
            self._removed.discard(state.icao)
            self._append_event(
                state,
                "detected" if is_new else ("position" if position_added else "update"),
                update.received_at,
            )

    def _append_event(
        self, state: AircraftState, kind: str, timestamp: datetime
    ) -> None:
        self._event_sequence += 1
        self._events.append(
            {
                "id": self._event_sequence,
                "timestamp": timestamp.isoformat(),
                "kind": kind,
                "icao": state.icao,
                "callsign": state.callsign,
                "altitude_ft": state.altitude_ft,
                "speed_kt": state.speed_kt,
                "track_deg": (
                    state.track_deg
                    if state.track_deg is not None
                    else state.calculated_track_deg
                ),
                "vertical_rate_fpm": state.vertical_rate_fpm,
                "lat": state.lat,
                "lon": state.lon,
                "squawk": state.squawk,
                "text": _event_text(state, kind),
            }
        )

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


def _event_text(state: AircraftState, kind: str) -> str:
    prefix = {
        "detected": "Обнаружен борт",
        "position": "Позиция",
        "update": "Обновление",
        "lost": "Борт пропал",
    }.get(kind, "Сообщение")
    identity = f"{state.icao.upper()} {state.callsign or ''}".strip()
    details: list[str] = []
    if state.altitude_ft is not None:
        details.append(f"{state.altitude_ft} ft")
    if state.speed_kt is not None:
        details.append(f"{state.speed_kt:.0f} kt")
    if state.lat is not None and state.lon is not None:
        details.append(f"{state.lat:.5f}, {state.lon:.5f}")
    if state.squawk:
        details.append(f"SQ {state.squawk}")
    suffix = f" · {' · '.join(details)}" if details else ""
    return f"{prefix}: {identity}{suffix}"
