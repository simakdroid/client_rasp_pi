from datetime import UTC, datetime
from pathlib import Path

from app.raw_capture import RawCapture


def test_raw_capture_writes_jsonl_and_purges_old_days(tmp_path: Path) -> None:
    capture = RawCapture(tmp_path / "raw-capture", keep_days=3)
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    capture.append(
        {
            "id": 1,
            "timestamp": now.isoformat(),
            "raw": "*8D40621D58C382D690C8AC2863A7;",
            "df": 17,
            "df_label": "ADS-B",
            "icao": "40621d",
            "adsb_type": 11,
        }
    )

    path = tmp_path / "raw-capture" / "2026-09-03.jsonl"
    assert path.is_file()
    line = path.read_text(encoding="utf-8").strip()
    assert '"raw":"*8D40621D58C382D690C8AC2863A7;"' in line
    assert '"icao":"40621d"' in line
    assert "callsign" not in line

    old = tmp_path / "raw-capture" / "2026-08-20.jsonl"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text('{"id":0}\n', encoding="utf-8")
    capture.purge()
    assert not old.exists()
    assert path.exists()

    status = capture.status()
    assert status["enabled"] is True
    assert status["keep_days"] == 3
    assert "2026-09-03.jsonl" in status["files"]


def test_raw_capture_disabled_when_directory_missing() -> None:
    capture = RawCapture(None)
    capture.append({"id": 1, "timestamp": datetime.now(UTC).isoformat(), "raw": "*8D;"})
    assert capture.status() == {"enabled": False}
