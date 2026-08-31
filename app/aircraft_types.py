from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

ICAO_RE = re.compile(r"^[0-9a-f]{6}$")
TYPE_CODE_RE = re.compile(r"^[A-Z0-9]{2,6}$")
# readsb `type` is the reception class (mode_s, adsb_icao, …), not the airframe.
EMITTER_TYPES = frozenset({
    "ADSB_ICAO",
    "ADSB_ICAO_NT",
    "ADSB_OTHER",
    "ADSR_ICAO",
    "ADSR_OTHER",
    "TISB_ICAO",
    "TISB_OTHER",
    "TISB_TRACKFILE",
    "MLAT",
    "MODE_S",
    "OTHER",
})


def normalize_icao(value: object) -> str:
    icao = str(value or "").strip().lower().lstrip("~")
    if not ICAO_RE.fullmatch(icao):
        raise ValueError("ICAO must be a 24-bit hex address")
    return icao


def normalize_type_code(value: object) -> str:
    code = str(value or "").strip().upper()
    if not TYPE_CODE_RE.fullmatch(code) or code in EMITTER_TYPES:
        raise ValueError("Aircraft type must be 2–6 characters")
    return code


def is_airframe_type_code(value: object) -> bool:
    code = str(value or "").strip().upper()
    return bool(TYPE_CODE_RE.fullmatch(code) and code not in EMITTER_TYPES)


class AircraftTypeCatalog:
    """JSON file of ICAO hex → type, used only when ADS-B did not supply a type."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._entries: dict[str, dict[str, str]] = {}
        self._fingerprint: tuple[int, int] | None = None
        self.load()

    def load(self) -> bool:
        previous = dict(self._entries)
        if self.path is None or not self.path.is_file():
            self._entries = {}
            self._fingerprint = None
            return previous != self._entries
        try:
            self._fingerprint = self._file_fingerprint()
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Cannot read aircraft types %s: %s", self.path, exc)
            return False
        if not isinstance(payload, dict):
            LOGGER.warning("Aircraft types %s must be a JSON object of ICAO → type", self.path)
            return False
        entries: dict[str, dict[str, str]] = {}
        for raw_icao, raw_entry in payload.items():
            try:
                entries[normalize_icao(raw_icao)] = self._parse_entry(raw_entry)
            except ValueError:
                LOGGER.warning("Skipping invalid aircraft type entry %r in %s", raw_icao, self.path)
                continue
        self._entries = entries
        return previous != self._entries

    def refresh(self) -> bool:
        fingerprint = self._file_fingerprint()
        if fingerprint == self._fingerprint:
            return False
        return self.load()

    def _file_fingerprint(self) -> tuple[int, int] | None:
        if self.path is None:
            return None
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def dump(self) -> dict[str, dict[str, str]]:
        return {icao: dict(entry) for icao, entry in sorted(self._entries.items())}

    def flush(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(self.dump(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)
        self._fingerprint = self._file_fingerprint()

    def list(self) -> list[dict[str, str]]:
        self.refresh()
        return [
            {"icao": icao.upper(), **entry}
            for icao, entry in sorted(self._entries.items())
        ]

    def lookup(self, icao: object) -> dict[str, str] | None:
        self.refresh()
        try:
            entry = self._entries.get(normalize_icao(icao))
        except ValueError:
            return None
        return dict(entry) if entry else None

    def upsert(
        self, icao: object, type_code: object, type_desc: object | None = None
    ) -> dict[str, str]:
        key = normalize_icao(icao)
        entry = {"type_code": normalize_type_code(type_code)}
        desc = str(type_desc or "").strip()
        if desc:
            entry["type_desc"] = desc[:80]
        self._entries[key] = entry
        self.flush()
        return {"icao": key.upper(), **entry}

    def delete(self, icao: object) -> bool:
        key = normalize_icao(icao)
        if key not in self._entries:
            return False
        del self._entries[key]
        self.flush()
        return True

    def apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.refresh()
        if is_airframe_type_code(payload.get("type_code")):
            return payload
        payload["type_code"] = None
        entry = self.lookup(payload.get("icao"))
        if not entry:
            return payload
        payload["type_code"] = entry["type_code"]
        if entry.get("type_desc") and not payload.get("type_desc"):
            payload["type_desc"] = entry["type_desc"]
        return payload

    def _parse_entry(self, raw_entry: object) -> dict[str, str]:
        if isinstance(raw_entry, str):
            return {"type_code": normalize_type_code(raw_entry)}
        if not isinstance(raw_entry, dict):
            raise ValueError("Invalid aircraft type entry")
        code = raw_entry.get("type_code") or raw_entry.get("t") or raw_entry.get("type")
        entry = {"type_code": normalize_type_code(code)}
        desc = str(raw_entry.get("type_desc") or raw_entry.get("desc") or "").strip()
        if desc:
            entry["type_desc"] = desc[:80]
        return entry
