from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from datetime import UTC, datetime
from typing import Any

from .mode_s import decode_avr

LOGGER = logging.getLogger(__name__)
HEX_DIGITS = frozenset("0123456789ABCDEF")


class RawMessageLog:
    def __init__(self, max_messages: int = 1000) -> None:
        self._messages: deque[dict[str, Any]] = deque(maxlen=max_messages)
        self._sequence = 0
        self._lock = asyncio.Lock()

    async def append(self, raw: str) -> None:
        decoded = decode_raw_line(raw)
        if decoded.get("df") == 0:
            return
        async with self._lock:
            self._sequence += 1
            entry = {
                "id": self._sequence,
                "timestamp": datetime.now(UTC).isoformat(),
                "raw": raw,
            }
            entry.update(decoded)
            self._messages.append(entry)

    async def recent(
        self,
        after_id: int = 0,
        before_id: int = 0,
        limit: int = 100,
        newest_first: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            filtered = [message for message in self._messages if message["id"] > after_id]
            if newest_first:
                window = (
                    filtered
                    if before_id <= 0
                    else [message for message in filtered if message["id"] < before_id]
                )
                page = list(reversed(window))[:limit]
                oldest = page[-1]["id"] if page else 0
                has_more = (
                    any(message["id"] < oldest for message in filtered)
                    if oldest
                    else bool(filtered)
                )
                return {
                    "messages": page,
                    "last_id": self._sequence,
                    "total": len(filtered),
                    "has_more": has_more,
                }
            return {
                "messages": filtered[-limit:],
                "last_id": self._sequence,
                "total": len(filtered),
                "has_more": len(filtered) > limit,
            }

    async def clear(self) -> dict[str, Any]:
        async with self._lock:
            self._messages.clear()
            return {"ok": True, "last_id": self._sequence}


async def ingest_raw_messages(host: str, port: int, message_log: RawMessageLog) -> None:
    delay = 1.0
    while True:
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.open_connection(host, port)
            LOGGER.info("Connected to readsb raw output at %s:%s", host, port)
            delay = 1.0
            while line := await reader.readline():
                if raw := normalize_raw_line(line.decode("latin-1", errors="replace")):
                    await message_log.append(raw)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            LOGGER.debug("Raw ADS-B stream unavailable: %s", exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()


def normalize_raw_line(line: str) -> str | None:
    raw = line.strip()
    return raw or None


def normalize_avr_message(line: str) -> str | None:
    return normalize_raw_line(line)


def decode_raw_line(raw: str) -> dict[str, Any]:
    if _is_avr_candidate(raw):
        try:
            return decode_avr(raw)
        except (ValueError, IndexError, TypeError):
            pass
    return {
        "df": None,
        "df_label": "сырой кадр",
        "icao": None,
        "callsign": None,
        "altitude_ft": None,
        "squawk": None,
        "adsb_type": None,
        "text": "Кадр без разбора Mode-S",
    }


def _is_avr_candidate(raw: str) -> bool:
    text = raw.strip()
    if len(text) < 3 or text[-1] != ";" or text[0] not in "*@!":
        return False
    body = text[1:-1]
    if text[0] == "@" and len(body) > 12:
        body = body[12:]
    return bool(body) and len(body) % 2 == 0 and all(
        character in HEX_DIGITS for character in body.upper()
    )
