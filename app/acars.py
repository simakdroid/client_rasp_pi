from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .tracker import _page_log

LOGGER = logging.getLogger(__name__)
MAX_TEXT = 400


class AcarsLog:
    def __init__(self, max_messages: int = 500) -> None:
        self._messages: deque[dict[str, Any]] = deque(maxlen=max_messages)
        self._sequence = 0
        self._generation = uuid4().hex
        self._lock = asyncio.Lock()

    async def append_datagram(self, raw: bytes) -> None:
        parsed = parse_acars_payload(raw)
        if parsed is None:
            return
        async with self._lock:
            self._sequence += 1
            parsed["id"] = self._sequence
            self._messages.append(parsed)

    async def recent(
        self,
        after_id: int = 0,
        before_id: int = 0,
        limit: int = 100,
        newest_first: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            return _page_log(
                list(self._messages),
                after_id=after_id,
                before_id=before_id,
                limit=limit,
                last_id=self._sequence,
                items_key="messages",
                newest_first=newest_first,
                generation=self._generation,
            )

    async def stats(self) -> dict[str, Any]:
        async with self._lock:
            last = self._messages[-1] if self._messages else None
            return {
                "count": len(self._messages),
                "last_id": self._sequence,
                "last_at": None if last is None else last.get("timestamp"),
            }

    async def clear(self) -> dict[str, Any]:
        async with self._lock:
            self._messages.clear()
            self._generation = uuid4().hex
            return {
                "ok": True,
                "last_id": self._sequence,
                "generation": self._generation,
            }


async def ingest_acars_udp(host: str, port: int, message_log: AcarsLog) -> None:
    loop = asyncio.get_running_loop()
    incoming: asyncio.Queue[bytes] = asyncio.Queue()

    class _Protocol(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, _addr: tuple[str, int]) -> None:
            incoming.put_nowait(data)

    transport, _ = await loop.create_datagram_endpoint(
        _Protocol, local_addr=(host, port)
    )
    LOGGER.info("Listening for ACARS JSON on UDP %s:%s", host, port)
    try:
        while True:
            await message_log.append_datagram(await incoming.get())
    finally:
        transport.close()


def parse_acars_payload(raw: bytes | str) -> dict[str, Any] | None:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace").strip()
    else:
        text = raw.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    flight = _text(payload.get("flight"))
    tail = _text(payload.get("tail") or payload.get("reg") or payload.get("registration"))
    label = _text(payload.get("label"))
    body = _text(payload.get("text") or payload.get("msg_text") or payload.get("message"))
    freq = _frequency(payload.get("freq") if "freq" in payload else payload.get("frequency"))
    error = payload.get("error")
    try:
        error_count = int(error) if error is not None else 0
    except (TypeError, ValueError):
        error_count = 0
    timestamp = _timestamp(payload.get("timestamp") or payload.get("time"))
    return {
        "timestamp": timestamp,
        "frequency_mhz": freq,
        "flight": flight,
        "tail": tail,
        "label": label,
        "mode": _text(payload.get("mode")),
        "block_id": _text(payload.get("block_id") or payload.get("bid")),
        "msgno": _text(payload.get("msgno") or payload.get("msgno_")),
        "error": error_count,
        "level": _number(payload.get("level")),
        "text": body,
        "summary": _summary(flight, tail, label, body),
    }


def _summary(
    flight: str | None, tail: str | None, label: str | None, body: str | None
) -> str:
    ident = flight or tail or "без позывного"
    if label and body:
        return f"{ident} · {label} · {body}"
    if label:
        return f"{ident} · {label}"
    if body:
        return f"{ident} · {body}"
    return ident


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {".", "?"}:
        return None
    return text[:MAX_TEXT]


def _number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number


def _frequency(value: object) -> float | None:
    number = _number(value)
    if number is None:
        return None
    if number > 1000:
        number /= 1000.0
    return round(number, 3)


def _timestamp(value: object) -> str:
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e12:
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, UTC).isoformat()
    text = _text(value)
    if text:
        return text
    return datetime.now(UTC).isoformat()
