from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .config import RadioChannel


class RadioMonitor:
    """Reports channel state without granting the web process systemd privileges."""

    def __init__(
        self,
        channels: list[RadioChannel],
        stats_path: Path | None = None,
        *,
        auto_detect: bool = True,
        min_receivers: int = 2,
        receiver_serial: str = "0118",
        sysfs_path: Path = Path("/sys/bus/usb/devices"),
    ) -> None:
        self.channels = channels
        self.stats_path = stats_path
        self.auto_detect = auto_detect
        self.min_receivers = min_receivers
        self.receiver_serial = receiver_serial
        self.sysfs_path = sysfs_path
        self._stats_mtime_ns: int | None = None
        self._counters: dict[str, float] = {}
        self._activity: dict[str, bool | None] = {}
        self._levels: dict[str, float] = {}

    async def status(self) -> list[dict[str, Any]]:
        if self.auto_detect and not await asyncio.to_thread(self.hardware_available):
            return []
        if self.stats_path:
            try:
                text, mtime_ns = await asyncio.to_thread(_read_stats, self.stats_path)
                self._apply_stats(text, mtime_ns)
            except OSError:
                pass
        return await asyncio.gather(*(self._channel_status(channel) for channel in self.channels))

    def hardware_available(self) -> bool:
        serials = _rtl_serials(self.sysfs_path)
        return len(serials) >= self.min_receivers and self.receiver_serial in serials

    async def _channel_status(self, channel: RadioChannel) -> dict[str, Any]:
        result: dict[str, Any] = {
            **channel.model_dump(exclude={"status_url"}),
            "active": self._activity.get(_frequency_key(channel.frequency_mhz)),
        }
        level = self._levels.get(_frequency_key(channel.frequency_mhz))
        if level is not None:
            result["level_dbfs"] = level
        if not channel.status_url:
            return result
        try:
            status = await asyncio.to_thread(_load_status, channel.status_url)
            result["active"] = bool(status.get("active"))
            result["level_dbfs"] = status.get("level_dbfs")
        except (OSError, ValueError, json.JSONDecodeError):
            result["status_error"] = True
        return result

    def _apply_stats(self, text: str, mtime_ns: int) -> None:
        if mtime_ns == self._stats_mtime_ns:
            return
        metrics = _parse_prometheus_stats(text)
        counters = metrics.get("channel_activity_counter", {})
        levels = metrics.get("channel_dbfs_signal_level", {})
        for frequency, counter in counters.items():
            previous = self._counters.get(frequency)
            self._activity[frequency] = None if previous is None else counter > previous
        self._counters = counters
        self._levels = levels
        self._stats_mtime_ns = mtime_ns


def _load_status(url: str) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "raspi-air-monitor/0.1"})
    with urlopen(request, timeout=1.5) as response:  # noqa: S310 - admin-configured URL
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("Radio status endpoint must return an object")
    return value


METRIC_RE = re.compile(
    r'^(channel_activity_counter|channel_dbfs_signal_level)\{[^}]*'
    r'freq="([^"]+)"[^}]*\}\s+([-+.\deE]+)$'
)


def _read_stats(path: Path) -> tuple[str, int]:
    text = path.read_text(encoding="utf-8")
    return text, path.stat().st_mtime_ns


def _parse_prometheus_stats(text: str) -> dict[str, dict[str, float]]:
    metrics: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        if match := METRIC_RE.match(line.strip()):
            name, frequency, value = match.groups()
            metrics.setdefault(name, {})[_frequency_key(float(frequency))] = float(value)
    return metrics


def _frequency_key(value: float) -> str:
    return f"{value:.3f}"


def _rtl_serials(sysfs_path: Path) -> list[str]:
    serials: list[str] = []
    try:
        devices = sysfs_path.iterdir()
    except OSError:
        return serials
    for device in devices:
        try:
            vendor = (device / "idVendor").read_text(encoding="ascii").strip().lower()
            product = (device / "idProduct").read_text(encoding="ascii").strip().lower()
            serial = (device / "serial").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if vendor == "0bda" and product in {"2832", "2838"} and serial:
            serials.append(serial)
    return serials
