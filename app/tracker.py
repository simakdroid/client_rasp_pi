from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pyproj import Geod

from .aircraft_types import AircraftTypeCatalog, is_airframe_type_code
from .coverage import CoverageRose
from .gis import LayerManager
from .mode_s import summary_text
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
        max_archive: int = 100,
        coverage_path: Path | None = None,
        max_coverage_km: float = 450,
        type_catalog: AircraftTypeCatalog | None = None,
    ) -> None:
        self.station_lat = station_lat
        self.station_lon = station_lon
        self.layer_manager = layer_manager
        self.ttl = timedelta(seconds=ttl_s)
        self.max_track_points = max_track_points
        self.min_track_distance_m = min_track_distance_m
        self.max_archive = max_archive
        self._aircraft: dict[str, AircraftState] = {}
        self._archive: OrderedDict[str, AircraftState] = OrderedDict()
        self._changed: set[str] = set()
        self._removed: set[str] = set()
        self._archive_evicted: set[str] = set()
        self._track_appends: dict[str, list[Position]] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._event_sequence = 0
        self._lock = asyncio.Lock()
        self._coverage = CoverageRose(
            station_lat, station_lon, coverage_path, max_coverage_km
        )
        self._type_catalog = type_catalog

    async def apply(self, updates: list[AircraftUpdate]) -> None:
        async with self._lock:
            for update in updates:
                self._merge(update)

    async def prune(self) -> None:
        threshold = datetime.now(UTC) - self.ttl
        now = datetime.now(UTC)
        async with self._lock:
            expired = [
                icao for icao, state in self._aircraft.items() if state.updated_at < threshold
            ]
            for icao in expired:
                state = self._aircraft.pop(icao)
                self._changed.discard(icao)
                self._track_appends.pop(icao, None)
                self._removed.add(icao)
                self._append_event(state, "lost", now)
                self._store_archive(state, now)

    async def coverage_snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return self._coverage.snapshot()

    async def reset_coverage(self) -> dict[str, Any]:
        async with self._lock:
            self._coverage.reset()
            self._coverage.flush()
            return self._coverage.snapshot()

    async def flush_coverage(self) -> None:
        async with self._lock:
            self._coverage.flush()

    async def attach_mode_s_context(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        async with self._lock:
            enriched: list[dict[str, Any]] = []
            for message in messages:
                item = dict(message)
                icao = str(item.get("icao") or "").lower()
                state = self._aircraft.get(icao) or self._archive.get(icao)
                if state:
                    item["known"] = True
                    item["distance_km"] = state.distance_km
                    if not item.get("callsign") and state.callsign:
                        item["callsign"] = state.callsign
                    if item.get("altitude_ft") is None:
                        item["altitude_ft"] = state.altitude_ft
                    if not item.get("squawk"):
                        item["squawk"] = state.squawk
                    item["text"] = summary_text(item)
                enriched.append(item)
            return enriched

    async def snapshot(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [
                self._export(state, include_track=True)
                for state in self._aircraft.values()
            ]

    async def archived_snapshot(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [
                self._export(state, include_track=False)
                for state in reversed(self._archive.values())
            ]

    async def recent_events(
        self,
        after_id: int = 0,
        before_id: int = 0,
        limit: int = 100,
        newest_first: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            return _page_log(
                list(self._events),
                after_id=after_id,
                before_id=before_id,
                limit=limit,
                last_id=self._event_sequence,
                items_key="events",
                newest_first=newest_first,
            )

    async def consume_delta(self) -> dict[str, Any] | None:
        async with self._lock:
            if not self._changed and not self._removed:
                return None
            changed = []
            for icao in self._changed:
                if icao not in self._aircraft:
                    continue
                item = self._export(self._aircraft[icao], include_track=False)
                if points := self._track_appends.get(icao):
                    item["track_append"] = [
                        [point.lat, point.lon, point.altitude_ft, point.timestamp.isoformat()]
                        for point in points
                    ]
                changed.append(item)
            archived = [
                self._export(self._archive[icao], include_track=False)
                for icao in self._removed
                if icao in self._archive
            ]
            result = {
                "type": "delta",
                "timestamp": datetime.now(UTC).isoformat(),
                "upsert": changed,
                "remove": sorted(self._removed),
                "archive": archived,
                "archive_remove": sorted(self._archive_evicted),
            }
            self._changed.clear()
            self._removed.clear()
            self._archive_evicted.clear()
            self._track_appends.clear()
            return result

    async def mark_type_changed(self, icao: str) -> None:
        key = icao.strip().lower()
        async with self._lock:
            if key in self._aircraft:
                self._changed.add(key)

    async def refresh_manual_types(self) -> None:
        if self._type_catalog is None or not self._type_catalog.refresh():
            return
        async with self._lock:
            for icao, state in self._aircraft.items():
                if is_airframe_type_code(state.type_code):
                    continue
                if self._type_catalog.lookup(icao):
                    self._changed.add(icao)

    def _export(self, state: AircraftState, include_track: bool = True) -> dict[str, Any]:
        payload = state.public_dict(include_track=include_track)
        if not is_airframe_type_code(payload.get("type_code")):
            payload["type_code"] = None
        if self._type_catalog:
            self._type_catalog.apply(payload)
        return payload

    def _store_archive(self, state: AircraftState, lost_at: datetime) -> None:
        if self.max_archive <= 0:
            return
        state.lost_at = lost_at
        self._archive.pop(state.icao, None)
        self._archive[state.icao] = state
        self._archive_evicted.discard(state.icao)
        while len(self._archive) > self.max_archive:
            evicted_id, _ = self._archive.popitem(last=False)
            self._archive_evicted.add(evicted_id)

    def _restore_archive(self, icao: str) -> AircraftState | None:
        state = self._archive.pop(icao, None)
        if state is None:
            return None
        state.lost_at = None
        self._archive_evicted.discard(icao)
        return state

    def _merge(self, update: AircraftUpdate) -> None:
        state = self._aircraft.get(update.icao)
        is_new = state is None
        if is_new:
            state = self._restore_archive(update.icao) or AircraftState(
                icao=update.icao,
                updated_at=update.received_at,
                started_at=update.received_at,
            )
            if state.started_at is None:
                state.started_at = update.received_at
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
            "type_code",
            "type_desc",
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

        if state.lat is not None and state.lon is not None:
            changed |= self._refresh_airspace(state)

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

    def _refresh_airspace(self, state: AircraftState) -> bool:
        assert state.lat is not None and state.lon is not None
        changed = False
        geofences = self.layer_manager.matching_geofences(
            state.lon, state.lat, state.altitude_ft
        )
        if geofences != state.geofences:
            state.geofences = geofences
            changed = True
        sector = self.layer_manager.matching_control_code(
            state.lon, state.lat, state.altitude_ft
        )
        if sector != state.sector:
            state.sector = sector
            changed = True
        return changed

    def _update_position(self, state: AircraftState, update: AircraftUpdate) -> bool:
        assert update.lat is not None and update.lon is not None
        changed = False
        azimuth_deg, _, distance_m = GEOD.inv(
            self.station_lon, self.station_lat, update.lon, update.lat
        )
        if not state.on_ground:
            self._coverage.observe(azimuth_deg, distance_m)
        azimuth = round(azimuth_deg % 360, 1)
        if azimuth == 360.0:
            azimuth = 0.0
        distance_km = round(distance_m / 1000, 2)
        if state.azimuth_deg != azimuth:
            state.azimuth_deg = azimuth
            changed = True
        if state.distance_km != distance_km:
            state.distance_km = distance_km
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


def _page_log(
    records: list[dict[str, Any]],
    *,
    after_id: int,
    before_id: int,
    limit: int,
    last_id: int,
    items_key: str,
    newest_first: bool,
) -> dict[str, Any]:
    filtered = [item for item in records if item["id"] > after_id]
    if newest_first:
        window = (
            filtered if before_id <= 0 else [item for item in filtered if item["id"] < before_id]
        )
        page = list(reversed(window))[:limit]
        oldest = page[-1]["id"] if page else 0
        has_more = any(item["id"] < oldest for item in filtered) if oldest else bool(filtered)
        return {
            items_key: page,
            "last_id": last_id,
            "total": len(filtered),
            "has_more": has_more,
        }
    return {
        items_key: filtered[-limit:],
        "last_id": last_id,
        "total": len(filtered),
        "has_more": len(filtered) > limit,
    }
