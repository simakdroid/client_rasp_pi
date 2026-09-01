from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
DAY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")


class SessionLog:
    """JSONL contact log on disk, one file per UTC day, kept for a few days."""

    def __init__(self, directory: Path | None = None, keep_days: int = 7) -> None:
        self.directory = directory
        self.keep_days = keep_days

    def record(self, payload: dict[str, Any]) -> None:
        if self.directory is None:
            return
        session_id = str(payload.get("id") or "").strip()
        started_at = _parse_time(payload.get("started_at"))
        if not session_id or started_at is None:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{started_at.date().isoformat()}.jsonl"
        entries = self._read_file(path)
        entries[session_id] = payload
        self._write_file(path, entries)
        self.purge()

    def record_many(self, payloads: list[dict[str, Any]]) -> None:
        for payload in payloads:
            self.record(payload)

    def list(self) -> list[dict[str, Any]]:
        if self.directory is None or not self.directory.is_dir():
            return []
        self.purge()
        cutoff = datetime.now(UTC) - timedelta(days=self.keep_days)
        sessions: dict[str, dict[str, Any]] = {}
        for path in sorted(self.directory.glob("*.jsonl")):
            if not DAY_FILE_RE.fullmatch(path.name):
                continue
            for session_id, payload in self._read_file(path).items():
                started_at = _parse_time(payload.get("started_at"))
                if started_at is None or started_at < cutoff:
                    continue
                sessions[session_id] = payload
        return sorted(
            sessions.values(),
            key=lambda item: str(item.get("started_at") or ""),
            reverse=True,
        )

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
                    LOGGER.warning("Cannot delete old session file %s: %s", path, exc)

    def _read_file(self, path: Path) -> dict[str, dict[str, Any]]:
        entries: dict[str, dict[str, Any]] = {}
        if not path.is_file():
            return entries
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            LOGGER.warning("Cannot read sessions %s: %s", path, exc)
            return entries
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                LOGGER.warning("Skipping invalid session line in %s", path)
                continue
            if not isinstance(payload, dict) or not payload.get("id"):
                continue
            entries[str(payload["id"])] = payload
        return entries

    def _write_file(self, path: Path, entries: dict[str, dict[str, Any]]) -> None:
        ordered = sorted(entries.values(), key=lambda item: str(item.get("started_at") or ""))
        temporary = path.with_name(f"{path.name}.tmp")
        try:
            temporary.write_text(
                "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in ordered),
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError as exc:
            LOGGER.warning("Cannot write sessions %s: %s", path, exc)
            if temporary.exists():
                temporary.unlink(missing_ok=True)


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
