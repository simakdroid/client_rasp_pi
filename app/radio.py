from __future__ import annotations

import asyncio
import contextlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

from .config import SCAN_MOUNTPOINT, RadioChannel

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


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
        return (
            len(serials) >= self.min_receivers
            and serials.count(self.receiver_serial) == 1
        )

    def connected_serials(self) -> list[str]:
        return _rtl_serials(self.sysfs_path)

    def role_snapshot(self, preferred_adsb: str = "1090") -> dict[str, Any]:
        serials = self.connected_serials()
        count = len(serials)
        vhf = self.receiver_serial
        duplicate = serials.count(vhf) > 1 or (
            bool(preferred_adsb) and serials.count(preferred_adsb) > 1
        )
        adsb_role: str | None = None
        vhf_role: str | None = None
        if count == 1:
            adsb_role = "0"
        elif (
            count >= 2
            and preferred_adsb
            and preferred_adsb != vhf
            and serials.count(preferred_adsb) == 1
            and serials.count(vhf) == 1
        ):
            adsb_role = preferred_adsb
            vhf_role = vhf
        return {
            "count": count,
            "serials": serials,
            "adsb": adsb_role,
            "vhf": vhf_role,
            "duplicate_serials": duplicate,
            "vhf_available": self.hardware_available(),
        }

    async def quality_snapshot(self) -> list[dict[str, Any]]:
        channels = await self.status()
        quality: list[dict[str, Any]] = []
        for channel in channels:
            quality.append(
                {
                    "id": channel.get("id"),
                    "name": channel.get("name"),
                    "frequency_mhz": channel.get("frequency_mhz"),
                    "active": channel.get("active"),
                    "level_dbfs": channel.get("level_dbfs"),
                    "status_error": bool(channel.get("status_error")),
                }
            )
        return quality

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


class IcecastStreamError(Exception):
    def __init__(self, message: str, *, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


async def open_icecast_audio(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    mount: str = SCAN_MOUNTPOINT,
    timeout_s: float = 4.0,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes, str]:
    """Connect to a local Icecast mount and return the raw MP3 body start."""
    target = (mount or SCAN_MOUNTPOINT).strip().lstrip("/")
    if target != SCAN_MOUNTPOINT:
        raise IcecastStreamError("неизвестный Icecast mount", status_code=404)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout_s,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise IcecastStreamError(
            f"Icecast не отвечает на {host}:{port}",
            status_code=503,
        ) from exc
    try:
        path = f"/{target}"
        writer.write(
            f"GET {path} HTTP/1.0\r\n"
            f"Host: {host}:{port}\r\n"
            "User-Agent: raspi-air-monitor\r\n"
            "Icy-MetaData: 0\r\n"
            "Accept: */*\r\n"
            "Connection: close\r\n"
            "\r\n".encode("ascii")
        )
        await asyncio.wait_for(writer.drain(), timeout=timeout_s)
        header_blob = b""
        while b"\r\n\r\n" not in header_blob and b"\n\n" not in header_blob:
            chunk = await asyncio.wait_for(reader.read(1024), timeout=timeout_s)
            if not chunk:
                break
            header_blob += chunk
            if len(header_blob) > 8192:
                raise IcecastStreamError("Icecast вернул слишком большой заголовок")
        separator = b"\r\n\r\n" if b"\r\n\r\n" in header_blob else b"\n\n"
        if separator not in header_blob:
            raise IcecastStreamError("Icecast не вернул HTTP-заголовки")
        raw_headers, leftover = header_blob.split(separator, 1)
        status_line, *header_lines = raw_headers.split(b"\n")
        status_text = status_line.decode("latin-1", errors="replace").strip()
        parts = status_text.split()
        try:
            status = int(parts[1]) if len(parts) >= 2 else 0
        except ValueError:
            status = 0
        if status != 200:
            raise IcecastStreamError(
                "поток vhf-scan.mp3 не подключён — rtl-airband не залогинен в Icecast",
                status_code=502,
            )
        content_type = "audio/mpeg"
        for line in header_lines:
            name, _, value = line.decode("latin-1", errors="replace").partition(":")
            if name.strip().lower() == "content-type" and value.strip():
                content_type = value.strip()
                break
        return reader, writer, leftover, content_type
    except IcecastStreamError:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        raise
    except (OSError, asyncio.TimeoutError) as exc:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        raise IcecastStreamError("Icecast оборвал соединение", status_code=502) from exc


def rewrite_loopback_stream_url(url: str, public_host: str) -> str:
    """Point Icecast URLs at the request host when the page is opened over LAN."""
    host = public_host.strip().lower().rstrip(".")
    if not host or host in _LOOPBACK_HOSTS:
        return url
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if hostname not in _LOOPBACK_HOSTS:
        return url
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]"
    else:
        netloc = host
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


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
