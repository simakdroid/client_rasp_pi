from __future__ import annotations

import asyncio
import json
import logging
import math
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from .aircraft_types import is_airframe_type_code
from .models import AircraftUpdate

LOGGER = logging.getLogger(__name__)
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_BATCH = 2000
MAX_SBS_LINE = 4096
SBS_CONNECT_TIMEOUT_S = 10.0


class AdsbSource(ABC):
    @abstractmethod
    async def updates(self) -> AsyncIterator[list[AircraftUpdate]]:
        """Yield batches of normalized aircraft updates until cancelled."""

    def health(self) -> dict[str, Any]:
        return {"status": "unknown"}


class ReadsbJsonSource(AdsbSource):
    def __init__(self, path: Path, interval_s: float, ttl_s: int) -> None:
        self.path = path
        self.interval_s = interval_s
        self.ttl_s = ttl_s
        self._last_fingerprint: tuple[Any, ...] | None = None
        self._health: dict[str, Any] = {"status": "starting"}

    def health(self) -> dict[str, Any]:
        return dict(self._health)

    async def updates(self) -> AsyncIterator[list[AircraftUpdate]]:
        while True:
            try:
                payload, mtime, size = await asyncio.to_thread(self._read)
                if not isinstance(payload, dict):
                    raise ValueError("aircraft.json root must be an object")
                generated = _number(payload.get("now"))
                reference_time = mtime
                if generated is not None:
                    try:
                        reference_time = datetime.fromtimestamp(generated, UTC)
                    except (OSError, OverflowError, ValueError):
                        pass
                fingerprint = (generated, mtime.timestamp(), size)
                dump_age_s = max(0.0, (datetime.now(UTC) - reference_time).total_seconds())
                if fingerprint == self._last_fingerprint:
                    self._health = {
                        "status": "stale" if dump_age_s > self.ttl_s else "live",
                        "detail": "unchanged snapshot",
                        "json_age_s": round(dump_age_s, 1),
                    }
                    yield []
                elif dump_age_s > self.ttl_s:
                    self._last_fingerprint = fingerprint
                    self._health = {
                        "status": "stale",
                        "detail": "aircraft.json is older than TTL",
                        "json_age_s": round(dump_age_s, 1),
                    }
                    LOGGER.warning("Ignoring stale %s (age %.1fs)", self.path, dump_age_s)
                    yield []
                else:
                    self._last_fingerprint = fingerprint
                    batch = _parse_readsb_batch(payload, reference_time, self.ttl_s)
                    self._health = {
                        "status": "live",
                        "json_age_s": round(dump_age_s, 1),
                        "batch": len(batch),
                    }
                    yield batch
            except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
                self._health = {"status": "unavailable", "detail": str(exc)}
                LOGGER.warning("Cannot read %s: %s", self.path, exc)
            await asyncio.sleep(self.interval_s)

    def _read(self) -> tuple[dict[str, Any], datetime, int]:
        data = self.path.read_bytes()
        if len(data) > MAX_JSON_BYTES:
            raise ValueError(f"aircraft.json exceeds {MAX_JSON_BYTES} bytes")
        payload = json.loads(data)
        mtime = datetime.fromtimestamp(self.path.stat().st_mtime, UTC)
        return payload, mtime, len(data)


class SbsSource(AdsbSource):
    def __init__(self, host: str, port: int, timezone: str = "UTC") -> None:
        self.host = host
        self.port = port
        self.timezone = timezone
        self._health: dict[str, Any] = {"status": "starting"}

    def health(self) -> dict[str, Any]:
        return dict(self._health)

    async def updates(self) -> AsyncIterator[list[AircraftUpdate]]:
        delay = 1.0
        while True:
            connected_at: float | None = None
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port),
                    timeout=SBS_CONNECT_TIMEOUT_S,
                )
                connected_at = asyncio.get_running_loop().time()
                self._health = {"status": "live", "detail": f"{self.host}:{self.port}"}
                LOGGER.info("Connected to SBS source at %s:%s", self.host, self.port)
                try:
                    while line := await reader.readline():
                        try:
                            update = parse_sbs_line(
                                line.decode("ascii", errors="ignore"),
                                timezone=self.timezone,
                            )
                        except (ValidationError, ValueError, TypeError, UnicodeError) as exc:
                            LOGGER.debug("Ignoring invalid SBS line: %s", exc)
                            continue
                        if update is not None:
                            yield [update]
                    LOGGER.warning("SBS source closed the connection")
                finally:
                    writer.close()
                    await writer.wait_closed()
            except (OSError, asyncio.IncompleteReadError, TimeoutError) as exc:
                self._health = {"status": "disconnected", "detail": str(exc)}
                LOGGER.warning("SBS source disconnected: %s; retry in %.1fs", exc, delay)
            else:
                self._health = {"status": "disconnected", "detail": "eof"}
            if connected_at is not None:
                uptime = asyncio.get_running_loop().time() - connected_at
                if uptime >= 30:
                    delay = 1.0
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


def _parse_readsb_batch(
    payload: dict[str, Any], reference_time: datetime, ttl_s: int
) -> list[AircraftUpdate]:
    raw_aircraft = payload.get("aircraft", [])
    if raw_aircraft is None:
        return []
    if not isinstance(raw_aircraft, list):
        raise ValueError("aircraft.json aircraft field must be an array")
    batch: list[AircraftUpdate] = []
    for raw in raw_aircraft[:MAX_BATCH]:
        if not isinstance(raw, dict):
            continue
        try:
            update = parse_readsb_aircraft(raw, reference_time, ttl_s)
        except (ValidationError, ValueError, TypeError) as exc:
            LOGGER.debug("Ignoring invalid readsb aircraft: %s", exc)
            continue
        if update is not None:
            batch.append(update)
    return batch


def parse_readsb_aircraft(
    raw: dict[str, Any], now: datetime, ttl_s: int
) -> AircraftUpdate | None:
    icao = str(raw.get("hex", "")).strip().lower()
    seen = _number(raw.get("seen"))
    if seen is None:
        seen = 0.0
    if seen < 0 or len(icao) != 6 or seen > ttl_s:
        return None
    try:
        # Reject non-hex and ~addresses before constructing the model.
        if icao.startswith("~"):
            return None
        int(icao, 16)
    except ValueError:
        return None
    altitude, altitude_reference, on_ground = _readsb_altitude(raw)
    vertical_rate_fpm, vertical_rate_source = _readsb_vertical_rate(raw)
    lat = _number(raw.get("lat"))
    lon = _number(raw.get("lon"))
    seen_pos = _number(raw.get("seen_pos"))
    position_at = None
    if lat is not None and lon is not None:
        pos_age = seen if seen_pos is None else seen_pos
        if pos_age < 0 or pos_age > ttl_s:
            lat = None
            lon = None
        else:
            position_at = now - timedelta(seconds=pos_age)
    return AircraftUpdate(
        icao=icao,
        callsign=_text(raw.get("flight"), 16),
        lat=lat,
        lon=lon,
        altitude_ft=altitude,
        altitude_reference=altitude_reference,
        speed_kt=_number(raw.get("gs", raw.get("tas"))),
        track_deg=_angle(raw.get("track")),
        true_heading_deg=_angle(raw.get("true_heading")),
        vertical_rate_fpm=vertical_rate_fpm,
        vertical_rate_source=vertical_rate_source,
        squawk=_text(raw.get("squawk"), 8),
        category=_text(raw.get("category"), 8),
        type_code=_type_code(raw.get("t")),
        type_desc=_text(raw.get("desc"), 80),
        on_ground=on_ground,
        seen_s=seen,
        received_at=now - timedelta(seconds=seen),
        position_at=position_at,
    )


def parse_sbs_line(line: str, timezone: str = "UTC") -> AircraftUpdate | None:
    if len(line) > MAX_SBS_LINE:
        return None
    fields = line.rstrip().split(",")
    if len(fields) < 22 or fields[0] != "MSG":
        return None
    icao = fields[4].strip().lower()
    if len(icao) != 6:
        return None
    try:
        int(icao, 16)
    except ValueError:
        return None
    timestamp = _parse_sbs_timestamp(fields[8], fields[9], timezone)
    lat = _number(fields[14])
    lon = _number(fields[15])
    position_at = timestamp if lat is not None and lon is not None else None
    altitude = _integer(fields[11])
    vertical_rate = _integer(fields[16])
    try:
        return AircraftUpdate(
            icao=icao,
            callsign=_text(fields[10], 16),
            altitude_ft=altitude,
            altitude_reference="baro" if altitude is not None else None,
            speed_kt=_number(fields[12]),
            track_deg=_angle(fields[13]),
            lat=lat,
            lon=lon,
            vertical_rate_fpm=vertical_rate,
            vertical_rate_source="baro" if vertical_rate is not None else None,
            squawk=_text(fields[17], 8),
            on_ground=_boolean(fields[21]),
            received_at=timestamp,
            position_at=position_at,
        )
    except (ValidationError, ValueError, TypeError):
        return None


def _parse_sbs_timestamp(date: str, time: str, timezone: str = "UTC") -> datetime:
    raw_time = time.strip()
    value = f"{date.strip()} {raw_time}"
    formats = (
        "%Y/%m/%d %H:%M:%S.%f",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    )
    zone = _sbs_zone(timezone)
    for fmt in formats:
        try:
            naive = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=zone).astimezone(UTC)
    LOGGER.debug("Cannot parse SBS timestamp %r %r; using receive time", date, time)
    return datetime.now(UTC)


def _sbs_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        LOGGER.warning("Unknown SBS timezone %s; using UTC", name)
        return ZoneInfo("UTC")


def _readsb_altitude(raw: dict[str, Any]) -> tuple[int | None, str | None, bool | None]:
    alt_baro = raw.get("alt_baro")
    if alt_baro == "ground":
        return None, None, True
    if alt_baro is not None:
        altitude = _integer(alt_baro)
        if altitude is None:
            return None, None, None
        return altitude, "baro", False
    altitude = _integer(raw.get("alt_geom"))
    if altitude is None:
        return None, None, None
    return altitude, "geom", False


def _readsb_vertical_rate(raw: dict[str, Any]) -> tuple[int | None, str | None]:
    if raw.get("baro_rate") is not None:
        rate = _integer(raw.get("baro_rate"))
        return (rate, "baro") if rate is not None else (None, None)
    if raw.get("geom_rate") is not None:
        rate = _integer(raw.get("geom_rate"))
        return (rate, "geom") if rate is not None else (None, None)
    return None, None


def _text(value: object, max_length: int = 80) -> str | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    return text[:max_length]


def _type_code(value: object) -> str | None:
    text = _text(value, 16)
    if text is None:
        return None
    code = text.upper()
    return code if is_airframe_type_code(code) else None


def _number(value: object) -> float | None:
    try:
        if value in (None, ""):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: object) -> int | None:
    number = _number(value)
    return None if number is None else round(number)


def _angle(value: object) -> float | None:
    number = _number(value)
    return None if number is None else number % 360


def _boolean(value: object) -> bool | None:
    if value in (None, ""):
        return None
    return str(value).strip().lower() in {"1", "true", "-1"}
