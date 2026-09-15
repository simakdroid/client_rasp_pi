"""Editable VHF channel list stored under /var/lib/adsb-vhf."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .config import RadioChannel, with_scan_stream
from .persist import atomic_write_json, read_json_file
from .rtl_airband_conf import parse_channels


def frequency_channel_id(frequency_mhz: float) -> str:
    return f"f{int(round(frequency_mhz * 1000)):06d}"


def stored_payload(channels: list[RadioChannel]) -> list[dict[str, object]]:
    return [
        {
            "id": channel.id,
            "name": channel.name,
            "frequency_mhz": channel.frequency_mhz,
        }
        for channel in channels
    ]


class RadioChannelCatalog:
    """JSON file of operator-edited VHF channels; env JSON is the first-run seed."""

    def __init__(self, path: Path | None = None, fallback_json: str = "[]") -> None:
        self.path = path
        self.fallback_json = fallback_json
        self._lock = threading.RLock()
        self._channels: list[RadioChannel] = []
        self.load()

    def channels(self) -> list[RadioChannel]:
        with self._lock:
            return list(self._channels)

    def public_list(self) -> list[dict[str, object]]:
        return [
            {
                "id": channel.id,
                "name": channel.name,
                "frequency_mhz": channel.frequency_mhz,
                "stream_url": channel.stream_url,
                "mountpoint": channel.mountpoint,
            }
            for channel in self.channels()
        ]

    def load(self) -> None:
        with self._lock:
            self._channels = with_scan_stream(self._read())
            if (
                self.path is not None
                and not self.path.is_file()
                and self._channels
            ):
                self._write_unlocked(self._channels)

    def upsert(self, name: str, frequency_mhz: float) -> RadioChannel:
        channel_id = frequency_channel_id(frequency_mhz)
        incoming = RadioChannel(id=channel_id, name=name.strip(), frequency_mhz=frequency_mhz)
        with self._lock:
            remaining = [
                item
                for item in self._channels
                if item.id != channel_id and item.frequency_mhz != incoming.frequency_mhz
            ]
            remaining.append(incoming)
            self._replace_unlocked(remaining)
            return next(item for item in self._channels if item.id == channel_id)

    def delete(self, channel_id: str) -> bool:
        key = str(channel_id or "").strip()
        with self._lock:
            remaining = [item for item in self._channels if item.id != key]
            if len(remaining) == len(self._channels):
                return False
            self._replace_unlocked(remaining)
            return True

    def _replace_unlocked(self, channels: list[RadioChannel]) -> None:
        if not channels:
            raise ValueError("нужна хотя бы одна VHF-частота")
        ordered = sorted(channels, key=lambda item: item.frequency_mhz)
        parse_channels(json.dumps(stored_payload(ordered)))
        self._write_unlocked(ordered)
        self._channels = with_scan_stream(ordered)

    def _read(self) -> list[RadioChannel]:
        if self.path is not None and self.path.is_file():
            payload = read_json_file(self.path)
        else:
            payload = json.loads(self.fallback_json or "[]")
        if payload in ([], None):
            return []
        if not isinstance(payload, list):
            raise ValueError("radio channel list must contain a JSON array")
        prepared = []
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError("each radio channel must be an object")
            row = dict(item)
            freq = row.get("frequency_mhz")
            if not row.get("id") and freq is not None:
                row["id"] = frequency_channel_id(float(freq))
            prepared.append(row)
        return parse_channels(json.dumps(prepared)) if prepared else []

    def _write_unlocked(self, channels: list[RadioChannel]) -> None:
        if self.path is None:
            return
        atomic_write_json(self.path, stored_payload(channels), indent=2, ensure_ascii=False)
