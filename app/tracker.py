from __future__ import annotations

import asyncio
import logging
import uuid
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
from .persist import atomic_write_json

LOGGER = logging.getLogger(__name__)
GEOD = Geod(ellps="WGS84")
GEOFENCE_LEAVE_MISSES = 2


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
        max_active_aircraft: int = 500,
    ) -> None:
        self.station_lat = station_lat
        self.station_lon = station_lon
        self.layer_manager = layer_manager
        self.ttl = timedelta(seconds=ttl_s)
        self.max_track_points = max_track_points
        self.min_track_distance_m = min_track_distance_m
        self.max_archive = max_archive
        self.max_active_aircraft = max_active_aircraft
        self._aircraft: dict[str, AircraftState] = {}
        self._archive: OrderedDict[str, AircraftState] = OrderedDict()
        self._contact_seq = 0
        self._changed: set[str] = set()
        self._removed: set[str] = set()
        self._archive_added: list[str] = []
        self._archive_evicted: set[str] = set()
        self._track_appends: dict[str, list[Position]] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._event_sequence = 0
        self._geofence_inside: dict[str, dict[str, dict[str, Any]]] = {}
        self._geofence_misses: dict[str, dict[str, int]] = {}
        self._generation = uuid.uuid4().hex
        self._log_generation = uuid.uuid4().hex
        self._seq = 0
        self._types_version = type_catalog.version if type_catalog is not None else -1
        self._lock = asyncio.Lock()
        self._coverage_io = asyncio.Lock()
        self._types_io = asyncio.Lock()
        self._resync_required = False
        self._max_pending_keys = max_active_aircraft + max_archive
        self._max_pending_track_points = max(max_track_points * 2, 64)
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
                self._coverage.forget(icao)
                self._forget_geofence_state(icao)
                self._append_event(state, "lost", now)
                self._store_archive(state, now)
            self._enforce_delta_budget()

    async def coverage_snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return self._coverage.snapshot()

    async def reset_coverage(self) -> dict[str, Any]:
        async with self._coverage_io:
            async with self._lock:
                self._coverage.reset()
                payload = self._coverage.dump()
                revision = self._coverage.revision
                path = self._coverage.path
                if path is None:
                    self._coverage.mark_saved(revision)
                    return self._coverage.snapshot()
            try:
                await asyncio.to_thread(atomic_write_json, path, payload)
            except OSError as exc:
                async with self._lock:
                    self._coverage.mark_save_error(str(exc))
                    return self._coverage.snapshot()
            async with self._lock:
                self._coverage.mark_saved(revision)
                return self._coverage.snapshot()

    async def flush_coverage(self) -> None:
        async with self._coverage_io:
            async with self._lock:
                if not self._coverage.dirty or self._coverage.path is None:
                    return
                payload = self._coverage.dump()
                revision = self._coverage.revision
                path = self._coverage.path
            try:
                await asyncio.to_thread(atomic_write_json, path, payload)
            except OSError as exc:
                async with self._lock:
                    self._coverage.mark_save_error(str(exc))
                return
            async with self._lock:
                self._coverage.mark_saved(revision)

    async def persist_type_upsert(
        self, icao: object, type_code: object, type_desc: object | None = None
    ) -> dict[str, str]:
        if self._type_catalog is None:
            raise RuntimeError("Aircraft type catalog is not configured")
        async with self._types_io:
            result = await asyncio.to_thread(
                self._type_catalog.upsert, icao, type_code, type_desc
            )
            self._types_version = self._type_catalog.version
            return result

    async def persist_type_delete(self, icao: object) -> bool:
        if self._type_catalog is None:
            raise RuntimeError("Aircraft type catalog is not configured")
        async with self._types_io:
            deleted = await asyncio.to_thread(self._type_catalog.delete, icao)
            self._types_version = self._type_catalog.version
            return deleted

    async def attach_mode_s_context(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        async with self._lock:
            enriched: list[dict[str, Any]] = []
            for message in messages:
                item = dict(message)
                icao = str(item.get("icao") or "").lower()
                state = self._aircraft.get(icao) or self._latest_archive(icao)
                if state:
                    item["known"] = True
                    df = item.get("df")
                    acas_reply = df in {0, 16}
                    if not acas_reply:
                        item["distance_km"] = state.distance_km
                    if not item.get("callsign") and state.callsign:
                        item["callsign"] = state.callsign
                    if not acas_reply and df not in {4, 20} and item.get("altitude_ft") is None:
                        item["altitude_ft"] = state.altitude_ft
                    if not acas_reply and not item.get("squawk"):
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

    async def snapshot_message(self) -> dict[str, Any]:
        async with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "generation": self._generation,
            "seq": self._seq,
            "timestamp": datetime.now(UTC).isoformat(),
            "aircraft": [
                self._export(state, include_track=True)
                for state in self._aircraft.values()
            ],
            "archived": [
                self._export(state, include_track=False)
                for state in reversed(self._archive.values())
            ],
        }

    async def archived_snapshot(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [
                self._export(state, include_track=False)
                for state in reversed(self._archive.values())
            ]

    async def position_stats(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        async with self._lock:
            ages = [
                max(0.0, (now - state.position_at).total_seconds())
                for state in self._aircraft.values()
                if state.position_at is not None
            ]
            return {
                "live": len(self._aircraft),
                "with_position": len(ages),
                "newest_position_age_s": round(min(ages), 1) if ages else None,
                "oldest_position_age_s": round(max(ages), 1) if ages else None,
            }

    async def recent_events(
        self,
        after_id: int = 0,
        before_id: int = 0,
        limit: int = 100,
        newest_first: bool = False,
        kinds: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            records = list(self._events)
            if kinds:
                allowed = set(kinds)
                records = [item for item in records if item.get("kind") in allowed]
            return _page_log(
                records,
                after_id=after_id,
                before_id=before_id,
                limit=limit,
                last_id=self._event_sequence,
                items_key="events",
                newest_first=newest_first,
                generation=self._log_generation,
            )

    async def clear_events(self) -> dict[str, Any]:
        async with self._lock:
            self._events.clear()
            self._log_generation = uuid.uuid4().hex
            return {
                "ok": True,
                "last_id": self._event_sequence,
                "generation": self._log_generation,
            }

    async def consume_delta(self) -> dict[str, Any] | None:
        async with self._lock:
            if self._resync_required:
                self._resync_required = False
                self._changed.clear()
                self._removed.clear()
                self._archive_added.clear()
                self._archive_evicted.clear()
                self._track_appends.clear()
                return {
                    "type": "resync",
                    "generation": self._generation,
                    "seq": self._seq,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            if (
                not self._changed
                and not self._removed
                and not self._archive_added
                and not self._archive_evicted
            ):
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
                self._export(self._archive[contact_id], include_track=False)
                for contact_id in self._archive_added
                if contact_id in self._archive
            ]
            result = {
                "type": "delta",
                "generation": self._generation,
                "seq": self._seq + 1,
                "timestamp": datetime.now(UTC).isoformat(),
                "upsert": changed,
                "remove": sorted(self._removed),
                "archive": archived,
                "archive_remove": sorted(self._archive_evicted),
            }
            self._seq += 1
            self._changed.clear()
            self._removed.clear()
            self._archive_added.clear()
            self._archive_evicted.clear()
            self._track_appends.clear()
            return result

    async def mark_type_changed(self, icao: str) -> None:
        key = icao.strip().lower()
        async with self._lock:
            self._touch_type_consumers_locked(icao=key, only_fallback=False)

    async def refresh_manual_types(self) -> None:
        if self._type_catalog is None:
            return
        async with self._types_io:
            await asyncio.to_thread(self._type_catalog.refresh)
            version = self._type_catalog.version
            if version == self._types_version:
                return
            self._types_version = version
        async with self._lock:
            self._touch_type_consumers_locked(icao=None, only_fallback=True)

    def _touch_type_consumers_locked(
        self, *, icao: str | None, only_fallback: bool
    ) -> None:
        for key, state in self._aircraft.items():
            if icao is not None and key != icao:
                continue
            if only_fallback and is_airframe_type_code(state.type_code):
                continue
            state.revision += 1
            self._changed.add(key)
        for contact_id, state in self._archive.items():
            if icao is not None and state.icao != icao:
                continue
            if only_fallback and is_airframe_type_code(state.type_code):
                continue
            state.revision += 1
            if contact_id not in self._archive_added:
                self._archive_added.append(contact_id)

    async def refresh_geofences(self) -> None:
        async with self._lock:
            for state in self._aircraft.values():
                if state.lat is None or state.lon is None:
                    continue
                if self._refresh_airspace(state):
                    state.revision += 1
                    self._changed.add(state.icao)

    def _export(self, state: AircraftState, include_track: bool = True) -> dict[str, Any]:
        payload = state.public_dict(include_track=include_track)
        if not is_airframe_type_code(payload.get("type_code")):
            payload["type_code"] = None
        if self._type_catalog:
            self._type_catalog.apply(payload)
        return payload

    def _enforce_delta_budget(self) -> None:
        if self._resync_required:
            return
        pending_keys = len(self._removed) + len(self._archive_added) + len(self._archive_evicted)
        pending_points = sum(len(points) for points in self._track_appends.values())
        over_keys = pending_keys > self._max_pending_keys
        over_points = pending_points > self._max_pending_track_points
        if not over_keys and not over_points:
            return
        self._mark_stream_resync()

    def _mark_stream_resync(self) -> None:
        self._generation = uuid.uuid4().hex
        self._seq = 0
        self._resync_required = True
        self._changed.clear()
        self._removed.clear()
        self._archive_added.clear()
        self._archive_evicted.clear()
        self._track_appends.clear()

    def _store_archive(self, state: AircraftState, lost_at: datetime) -> None:
        if self.max_archive <= 0:
            return
        if not state.contact_id:
            state.contact_id = self._next_contact_id(state.icao)
        state.lost_at = lost_at
        self._archive[state.contact_id] = state
        self._archive_added.append(state.contact_id)
        self._archive_evicted.discard(state.contact_id)
        while len(self._archive) > self.max_archive:
            evicted_id, _ = self._archive.popitem(last=False)
            self._archive_evicted.add(evicted_id)

    def _latest_archive(self, icao: str) -> AircraftState | None:
        for state in reversed(self._archive.values()):
            if state.icao == icao:
                return state
        return None

    def _next_contact_id(self, icao: str) -> str:
        self._contact_seq += 1
        return f"{icao}-{self._contact_seq}"

    def _restore_archive(self, update: AircraftUpdate) -> AircraftState | None:
        previous = self._latest_archive(update.icao)
        if previous is None:
            return None
        if _should_start_new_contact(
            previous, update, resume_after=self.ttl * 3
        ):
            return AircraftState(
                icao=update.icao,
                category=previous.category,
                type_code=previous.type_code,
                type_desc=previous.type_desc,
            )
        self._archive.pop(previous.contact_id, None)
        if previous.contact_id:
            self._archive_evicted.add(previous.contact_id)
        return AircraftState(
            icao=update.icao,
            callsign=previous.callsign,
            squawk=previous.squawk,
            category=previous.category,
            type_code=previous.type_code,
            type_desc=previous.type_desc,
        )

    def _merge(self, update: AircraftUpdate) -> None:
        state = self._aircraft.get(update.icao)
        is_new = state is None
        if is_new:
            if len(self._aircraft) >= self.max_active_aircraft:
                LOGGER.warning(
                    "Active aircraft limit %s reached; dropping %s",
                    self.max_active_aircraft,
                    update.icao,
                )
                return
            state = self._restore_archive(update) or AircraftState(
                icao=update.icao,
                updated_at=update.received_at,
                started_at=update.received_at,
            )
            state.started_at = update.received_at
            state.updated_at = update.received_at
            if not state.contact_id:
                state.contact_id = self._next_contact_id(update.icao)
            self._aircraft[update.icao] = state
            self._forget_geofence_state(update.icao)

        changed = is_new
        position_added = False
        message_at = update.received_at
        if _is_newer(state.last_message_at, message_at):
            state.last_message_at = message_at
        state.updated_at = max(state.updated_at, message_at)

        changed |= self._merge_group(
            state,
            update,
            ("callsign", "squawk", "category", "type_code", "type_desc"),
            "identity_at",
            message_at,
        )
        changed |= self._merge_group(
            state,
            update,
            ("altitude_ft", "altitude_reference", "on_ground"),
            "altitude_at",
            message_at,
        )
        changed |= self._merge_group(
            state,
            update,
            (
                "speed_kt",
                "track_deg",
                "true_heading_deg",
                "vertical_rate_fpm",
                "vertical_rate_source",
            ),
            "velocity_at",
            message_at,
        )

        position_at = update.position_at or (
            message_at if update.lat is not None and update.lon is not None else None
        )
        if (
            update.lat is not None
            and update.lon is not None
            and position_at is not None
            and _is_newer(state.position_at, position_at)
        ):
            if state.lat != update.lat:
                state.lat = update.lat
                changed = True
            if state.lon != update.lon:
                state.lon = update.lon
                changed = True
            previous_track_time = state.track[-1].timestamp if state.track else None
            changed |= self._update_position(state, update, position_at)
            state.position_at = position_at
            if state.track and state.track[-1].timestamp != previous_track_time:
                appends = self._track_appends.setdefault(state.icao, [])
                appends.append(state.track[-1])
                overflow = len(appends) - self.max_track_points
                if overflow > 0:
                    del appends[:overflow]
                position_added = True
                self._enforce_delta_budget()

        fence_events: list[tuple[str, dict[str, Any]]] = []
        if state.lat is not None and state.lon is not None:
            changed |= self._refresh_airspace(state, fence_events)

        if changed:
            state.revision += 1
            self._changed.add(state.icao)
            self._removed.discard(state.icao)
            self._append_event(
                state,
                "detected" if is_new else ("position" if position_added else "update"),
                update.received_at,
            )
        for kind, extra in fence_events:
            self._append_event(
                state, kind, state.position_at or update.received_at, extra
            )

    def _merge_group(
        self,
        state: AircraftState,
        update: AircraftUpdate,
        names: tuple[str, ...],
        time_attr: str,
        incoming_at: datetime,
    ) -> bool:
        if not any(getattr(update, name) is not None for name in names):
            return False
        current_at = getattr(state, time_attr)
        if not _is_newer(current_at, incoming_at):
            return False
        changed = False
        for name in names:
            value = getattr(update, name)
            if value is not None and value != getattr(state, name):
                setattr(state, name, value)
                changed = True
        setattr(state, time_attr, incoming_at)
        return changed

    def _append_event(
        self,
        state: AircraftState,
        kind: str,
        timestamp: datetime,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._event_sequence += 1
        payload: dict[str, Any] = {
            "id": self._event_sequence,
            "timestamp": timestamp.isoformat(),
            "kind": kind,
            "icao": state.icao,
            "callsign": state.callsign,
            "altitude_ft": state.altitude_ft,
            "altitude_reference": state.altitude_reference,
            "speed_kt": state.speed_kt,
            "track_deg": (
                state.track_deg
                if state.track_deg is not None
                else state.calculated_track_deg
            ),
            "true_heading_deg": state.true_heading_deg,
            "vertical_rate_fpm": state.vertical_rate_fpm,
            "vertical_rate_source": state.vertical_rate_source,
            "lat": state.lat,
            "lon": state.lon,
            "squawk": state.squawk,
            "text": _event_text(state, kind, extra),
        }
        if extra:
            for key, value in extra.items():
                if value is not None:
                    payload[key] = value
        self._events.append(payload)

    def _forget_geofence_state(self, icao: str) -> None:
        self._geofence_inside.pop(icao, None)
        self._geofence_misses.pop(icao, None)

    def _refresh_airspace(
        self,
        state: AircraftState,
        pending_events: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> bool:
        assert state.lat is not None and state.lon is not None
        _, sector, matched = self.layer_manager.match_airspace(
            state.lon, state.lat, state.altitude_ft
        )
        catalog_version = self.layer_manager.catalog.version
        timestamp = state.position_at or state.updated_at
        inside = self._geofence_inside.setdefault(state.icao, {})
        misses = self._geofence_misses.setdefault(state.icao, {})
        matched_by_key = {item.key: item for item in matched}
        changed = False

        def emit(kind: str, extra: dict[str, Any]) -> None:
            if pending_events is None:
                self._append_event(state, kind, timestamp, extra)
            else:
                pending_events.append((kind, extra))

        for key, geofence in matched_by_key.items():
            misses.pop(key, None)
            if key in inside:
                continue
            inside[key] = {
                "zone": geofence.name,
                "layer_id": geofence.layer_id,
                "catalog_version": catalog_version,
            }
            emit(
                "geofence_enter",
                {
                    "zone": geofence.name,
                    "geofence_key": key,
                    "layer_id": geofence.layer_id,
                    "catalog_version": catalog_version,
                    "altitude_ft": state.altitude_ft,
                    "altitude_reference": state.altitude_reference,
                    "hysteresis_leave_after": GEOFENCE_LEAVE_MISSES,
                },
            )
            changed = True

        for key in list(inside):
            if key in matched_by_key:
                continue
            misses[key] = misses.get(key, 0) + 1
            misses_count = misses[key]
            if misses_count < GEOFENCE_LEAVE_MISSES:
                continue
            info = inside.pop(key)
            misses.pop(key, None)
            emit(
                "geofence_leave",
                {
                    "zone": info.get("zone"),
                    "geofence_key": key,
                    "layer_id": info.get("layer_id"),
                    "catalog_version": catalog_version,
                    "altitude_ft": state.altitude_ft,
                    "altitude_reference": state.altitude_reference,
                    "hysteresis_misses": misses_count,
                    "hysteresis_leave_after": GEOFENCE_LEAVE_MISSES,
                },
            )
            changed = True

        geofences = {str(item["zone"]) for item in inside.values() if item.get("zone")}
        geofence_ids = set(inside)
        if geofences != state.geofences:
            state.geofences = geofences
            changed = True
        if geofence_ids != state.geofence_ids:
            state.geofence_ids = geofence_ids
            changed = True
        if sector != state.sector:
            state.sector = sector
            changed = True
        return changed

    def _update_position(
        self, state: AircraftState, update: AircraftUpdate, position_at: datetime
    ) -> bool:
        assert update.lat is not None and update.lon is not None
        changed = False
        azimuth_deg, _, distance_m = GEOD.inv(
            self.station_lon, self.station_lat, update.lon, update.lat
        )
        if state.on_ground is False:
            self._coverage.observe(
                azimuth_deg,
                distance_m,
                icao=state.icao,
                lat=update.lat,
                lon=update.lon,
                altitude_ft=(
                    update.altitude_ft
                    if update.altitude_ft is not None
                    else state.altitude_ft
                ),
            )
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
            timestamp=position_at,
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
            state.vertical_rate_source = "derived"
        state.track.append(point)
        if len(state.track) > self.max_track_points:
            del state.track[: len(state.track) - self.max_track_points]
        return True


def _is_newer(current: datetime | None, incoming: datetime) -> bool:
    return current is None or incoming > current


def _normalized_callsign(value: str | None) -> str | None:
    if not value:
        return None
    compact = " ".join(value.split()).upper()
    return compact or None


def _should_start_new_contact(
    previous: AircraftState,
    update: AircraftUpdate,
    *,
    resume_after: timedelta,
) -> bool:
    """Same contact after a short gap; a long gap or conflicting identity starts a new one."""
    if previous.lost_at is not None and (update.received_at - previous.lost_at) > resume_after:
        return True
    incoming_squawk = (update.squawk or "").strip() or None
    previous_squawk = (previous.squawk or "").strip() or None
    incoming_callsign = _normalized_callsign(update.callsign)
    previous_callsign = _normalized_callsign(previous.callsign)
    if incoming_squawk and previous_squawk and incoming_squawk != previous_squawk:
        return True
    if incoming_callsign and previous_callsign and incoming_callsign != previous_callsign:
        return True
    if not incoming_callsign and not previous_callsign:
        return True
    if not incoming_squawk and not previous_squawk:
        return True
    return False


def _event_text(
    state: AircraftState, kind: str, extra: dict[str, Any] | None = None
) -> str:
    extra = extra or {}
    zone = str(extra.get("zone") or "").strip()
    prefix = {
        "detected": "Обнаружен борт",
        "position": "Позиция",
        "update": "Обновление",
        "lost": "Борт пропал",
        "geofence_enter": f"Вход в зону {zone}".strip(),
        "geofence_leave": f"Выход из зоны {zone}".strip(),
    }.get(kind, "Сообщение")
    identity = f"{state.icao.upper()} {state.callsign or ''}".strip()
    details: list[str] = []
    altitude_ft = extra.get("altitude_ft", state.altitude_ft)
    altitude_reference = extra.get("altitude_reference", state.altitude_reference)
    if altitude_ft is not None:
        reference = {
            "baro": "баро",
            "geom": "геом",
        }.get(str(altitude_reference or ""), "")
        altitude = f"{altitude_ft} ft"
        if reference:
            altitude = f"{altitude} ({reference})"
        details.append(altitude)
    if extra.get("catalog_version") is not None:
        details.append(f"каталог v{extra['catalog_version']}")
    if kind == "geofence_leave" and extra.get("hysteresis_leave_after") is not None:
        details.append(
            f"гистерезис {extra.get('hysteresis_misses')}/{extra.get('hysteresis_leave_after')}"
        )
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
    generation: str,
) -> dict[str, Any]:
    oldest_id = records[0]["id"] if records else 0
    filtered = [item for item in records if item["id"] > after_id]
    truncated = bool(records) and oldest_id > 1
    if newest_first:
        window = (
            filtered if before_id <= 0 else [item for item in filtered if item["id"] < before_id]
        )
        page = list(reversed(window))[:limit]
        oldest = page[-1]["id"] if page else 0
        has_more = bool(page) and any(item["id"] < oldest for item in window)
        next_before = oldest if page else before_id
        return {
            items_key: page,
            "generation": generation,
            "last_id": last_id,
            "latest_id": last_id,
            "oldest_id": oldest_id,
            "next_after_id": page[0]["id"] if page else after_id,
            "next_before_id": next_before,
            "total": len(filtered),
            "has_more": has_more,
            "truncated": truncated,
            "mode": "before",
        }
    # after_id=0 → latest N; after_id>0 → sequential catch-up without skipping.
    page = filtered[:limit] if after_id > 0 else filtered[-limit:]
    return {
        items_key: page,
        "generation": generation,
        "last_id": last_id,
        "latest_id": last_id,
        "oldest_id": oldest_id,
        "next_after_id": page[-1]["id"] if page else after_id,
        "total": len(filtered),
        "has_more": len(filtered) > limit,
        "truncated": truncated,
        "mode": "since" if after_id > 0 else "latest",
    }
