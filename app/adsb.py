from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import AircraftUpdate

LOGGER = logging.getLogger(__name__)


class AdsbSource(ABC):
    @abstractmethod
    async def updates(self) -> AsyncIterator[list[AircraftUpdate]]:
        """Yield batches of normalized aircraft updates until cancelled."""


class ReadsbJsonSource(AdsbSource):
    def __init__(self, path: Path, interval_s: float, ttl_s: int) -> None:
        self.path = path
        self.interval_s = interval_s
        self.ttl_s = ttl_s

    async def updates(self) -> AsyncIterator[list[AircraftUpdate]]:
        while True:
            try:
                payload, mtime = await asyncio.to_thread(self._read)
                generated = _number(payload.get("now"))
                reference_time = mtime
                if generated is not None:
                    try:
                        reference_time = datetime.fromtimestamp(generated, UTC)
                    except (OSError, OverflowError, ValueError):
                        pass
                batch = [
                    update
                    for raw in payload.get("aircraft", [])
                    if (
                        update := parse_readsb_aircraft(raw, reference_time, self.ttl_s)
                    )
                    is not None
                ]
                yield batch
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                LOGGER.warning("Cannot read %s: %s", self.path, exc)
            await asyncio.sleep(self.interval_s)

    def _read(self) -> tuple[dict[str, Any], datetime]:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        mtime = datetime.fromtimestamp(self.path.stat().st_mtime, UTC)
        return payload, mtime


class SbsSource(AdsbSource):
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    async def updates(self) -> AsyncIterator[list[AircraftUpdate]]:
        delay = 1.0
        while True:
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
                LOGGER.info("Connected to SBS source at %s:%s", self.host, self.port)
                delay = 1.0
                try:
                    while line := await reader.readline():
                        update = parse_sbs_line(line.decode("ascii", errors="ignore"))
                        if update is not None:
                            yield [update]
                finally:
                    writer.close()
                    await writer.wait_closed()
            except (OSError, asyncio.IncompleteReadError) as exc:
                LOGGER.warning("SBS source disconnected: %s; retry in %.1fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)


def parse_readsb_aircraft(
    raw: dict[str, Any], now: datetime, ttl_s: int
) -> AircraftUpdate | None:
    icao = str(raw.get("hex", "")).strip().lower()
    seen = _number(raw.get("seen")) or 0.0
    if len(icao) != 6 or seen > ttl_s:
        return None
    altitude_raw = raw.get("alt_baro", raw.get("alt_geom"))
    altitude = None if altitude_raw in (None, "ground") else _integer(altitude_raw)
    return AircraftUpdate(
        icao=icao,
        callsign=_text(raw.get("flight")),
        lat=_number(raw.get("lat")),
        lon=_number(raw.get("lon")),
        altitude_ft=altitude,
        speed_kt=_number(raw.get("gs", raw.get("tas"))),
        track_deg=_angle(raw.get("track", raw.get("true_heading"))),
        vertical_rate_fpm=_integer(raw.get("baro_rate", raw.get("geom_rate"))),
        squawk=_text(raw.get("squawk")),
        category=_text(raw.get("category")),
        type_code=_type_code(raw.get("t") or raw.get("type")),
        type_desc=_text(raw.get("desc")),
        on_ground=altitude_raw == "ground",
        seen_s=seen,
        received_at=now - timedelta(seconds=seen),
    )


def parse_sbs_line(line: str) -> AircraftUpdate | None:
    fields = line.rstrip().split(",")
    if len(fields) < 22 or fields[0] != "MSG":
        return None
    icao = fields[4].strip().lower()
    if len(icao) != 6:
        return None
    timestamp = _parse_sbs_timestamp(fields[8], fields[9])
    return AircraftUpdate(
        icao=icao,
        callsign=_text(fields[10]),
        altitude_ft=_integer(fields[11]),
        speed_kt=_number(fields[12]),
        track_deg=_angle(fields[13]),
        lat=_number(fields[14]),
        lon=_number(fields[15]),
        vertical_rate_fpm=_integer(fields[16]),
        squawk=_text(fields[17]),
        on_ground=_boolean(fields[21]),
        received_at=timestamp,
    )


def _parse_sbs_timestamp(date: str, time: str) -> datetime:
    value = f"{date.strip()} {time.strip().split('.')[0]}"
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def _text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _type_code(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    code = text.upper()
    return code if 2 <= len(code) <= 6 else None


def _number(value: object) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


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
