from __future__ import annotations

import json
import platform
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings


def read_adsb_status(path: Path) -> dict[str, Any]:
    """Return readsb JSON health without treating zero aircraft as a failure."""
    if not path.is_file():
        return {
            "status": "unavailable",
            "detail": f"{path} does not exist",
            "aircraft": 0,
        }

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "invalid",
            "detail": str(exc),
            "aircraft": 0,
        }

    generated_at = _generated_at(payload, path)
    age_s = max(0.0, (datetime.now(UTC) - generated_at).total_seconds())
    aircraft = payload.get("aircraft")
    aircraft_count = len(aircraft) if isinstance(aircraft, list) else 0
    return {
        "status": "online" if age_s <= 5 else "stale",
        "json_age_s": round(age_s, 1),
        "messages": payload.get("messages"),
        "aircraft": aircraft_count,
    }


def _generated_at(payload: dict[str, Any], path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(float(payload["now"]), UTC)
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)


def read_host_status(path: Path) -> dict[str, Any]:
    """Best-effort disk and CPU temperature; omitted where the OS has no data."""
    status: dict[str, Any] = {}
    try:
        usage = shutil.disk_usage(path)
        status["disk_free_gb"] = round(usage.free / 1_000_000_000, 2)
        status["disk_total_gb"] = round(usage.total / 1_000_000_000, 2)
    except OSError:
        pass
    thermal = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        status["cpu_temp_c"] = round(int(thermal.read_text(encoding="ascii").strip()) / 1000, 1)
    except (OSError, ValueError):
        pass
    return status


def collect_diagnostics(
    *,
    settings: Settings,
    health: dict[str, Any],
    gis: dict[str, Any],
    coverage: dict[str, Any],
    host: dict[str, Any],
    radio: dict[str, Any] | None = None,
    positions: dict[str, Any] | None = None,
    session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a support dump without tokens, passwords or stream URLs."""
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "app": {
            "name": settings.app_name,
            "version": _app_version(),
            "python": sys.version.split()[0],
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "settings": _public_settings(settings),
        "health": {
            "status": health.get("status"),
            "live": health.get("live"),
            "ready": health.get("ready"),
            "time": health.get("time"),
            "tasks": health.get("tasks"),
            "source": health.get("source"),
            "last_batch_at": health.get("last_batch_at"),
            "adsb": health.get("adsb"),
            "source_mode": health.get("source_mode"),
        },
        "positions": positions or {},
        "gis": {
            "version": gis.get("version"),
            "last_good_version": gis.get("last_good_version"),
            "loaded_at": gis.get("loaded_at"),
            "load_ms": gis.get("load_ms"),
            "feature_count": gis.get("feature_count"),
            "layer_count": gis.get("layer_count", len(gis.get("layers") or [])),
            "geofence_count": gis.get("geofence_count"),
            "error_count": len(gis.get("errors") or []),
            "errors": gis.get("errors") or [],
        },
        "coverage": {
            "kind": coverage.get("kind"),
            "caption": coverage.get("caption"),
            "saved": coverage.get("saved"),
            "save_error": coverage.get("save_error"),
            "load_error": coverage.get("load_error"),
            "filled_bins": coverage.get("filled_bins"),
            "max_range_km": coverage.get("max_range_km"),
            "observations": coverage.get("observations"),
            "range_updates": coverage.get("range_updates"),
            "altitude_bands": coverage.get("altitude_bands"),
            "hourly": coverage.get("hourly"),
        },
        "radio": radio or {},
        "session": {
            "recording": (session or {}).get("recording"),
            "events": (session or {}).get("events"),
            "batches": (session or {}).get("batches"),
            "updates": (session or {}).get("updates"),
        },
        "host": host,
    }


def _app_version() -> str:
    return __version__


def _public_settings(settings: Settings) -> dict[str, Any]:
    channels: list[dict[str, Any]] = []
    try:
        channels = [
            {
                "id": channel.id,
                "name": channel.name,
                "frequency_mhz": channel.frequency_mhz,
            }
            for channel in settings.radio_channels
        ]
    except (ValueError, TypeError):
        channels = []
    return {
        "host": settings.host,
        "port": settings.port,
        "station_name": settings.station_name,
        "station_lat": settings.station_lat,
        "station_lon": settings.station_lon,
        "adsb_source": settings.adsb_source,
        "sbs_host": settings.sbs_host,
        "sbs_port": settings.sbs_port,
        "sbs_timezone": settings.sbs_timezone,
        "aircraft_ttl_s": settings.aircraft_ttl_s,
        "max_active_aircraft": settings.max_active_aircraft,
        "websocket_max_clients": settings.websocket_max_clients,
        "admin_required": bool(settings.admin_token),
        "trusted_hosts": list(settings.trusted_hosts),
        "layers_dir": str(settings.layers_dir),
        "ofm_configured": bool(settings.ofm_url),
        "radio_auto_detect": settings.radio_auto_detect,
        "radio_min_rtl_receivers": settings.radio_min_rtl_receivers,
        "adsb_preferred_serial": settings.adsb_preferred_serial,
        "radio_receiver_serial": settings.radio_receiver_serial,
        "radio_channels": channels,
    }
