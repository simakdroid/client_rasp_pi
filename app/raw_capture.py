from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
DAY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
CAPTURE_FIELDS = ("id", "timestamp", "raw", "df", "df_label", "icao", "adsb_type")


class RawCapture:
    """Append-only JSONL log of raw Mode-S frames; stays on the station disk only."""

    def __init__(self, directory: Path | None = None, keep_days: int = 3) -> None:
        self.directory = directory
        self.keep_days = keep_days
        self._append_count = 0

    def append(self, entry: dict[str, Any]) -> None:
        if self.directory is None:
            return
        timestamp = _parse_time(entry.get("timestamp"))
        if timestamp is None:
            return
        line = {key: entry[key] for key in CAPTURE_FIELDS if key in entry}
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{timestamp.date().isoformat()}.jsonl"
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
        except OSError as exc:
            LOGGER.warning("Cannot write raw capture %s: %s", path, exc)
            return
        self._append_count += 1
        if self._append_count % 5000 == 0:
            self.purge()

    def purge(self) -> None:
        if self.directory is None or not self.directory.is_dir():
            return
        cutoff = datetime.now(UTC).date() - timedelta(days=self.keep_days)
        for path in self.directory.glob("*.jsonl"):
            match = DAY_FILE_RE.fullmatch(path.name)
            if match is None:
                continue
            try:
                file_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            if file_date < cutoff:
                try:
                    path.unlink()
                except OSError as exc:
                    LOGGER.warning("Cannot delete old raw capture %s: %s", path, exc)

    def status(self) -> dict[str, Any]:
        if self.directory is None:
            return {"enabled": False}
        files = sorted(
            path.name
            for path in self.directory.glob("*.jsonl")
            if DAY_FILE_RE.fullmatch(path.name)
        )
        return {
            "enabled": True,
            "directory": str(self.directory),
            "keep_days": self.keep_days,
            "files": files[-7:],
        }


def _parse_time(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
