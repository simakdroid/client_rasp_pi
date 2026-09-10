from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

MAX_JSON_FILE_BYTES = 2 * 1024 * 1024


def read_json_file(path: Path, *, max_bytes: int = MAX_JSON_FILE_BYTES) -> Any:
    data = path.read_bytes()
    if len(data) > max_bytes:
        raise ValueError(f"{path} exceeds {max_bytes} bytes")
    return json.loads(data)


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    indent: int | None = None,
    ensure_ascii: bool = False,
) -> None:
    """Write JSON via a unique tempfile, fsync, then replace the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=indent, ensure_ascii=ensure_ascii)
            if indent is not None:
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
