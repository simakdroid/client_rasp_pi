import json
import time

from app.config import Settings
from app.diagnostics import collect_diagnostics, read_adsb_status


def test_adsb_status_distinguishes_zero_aircraft_from_failure(tmp_path) -> None:
    path = tmp_path / "aircraft.json"
    path.write_text(
        json.dumps({"now": time.time(), "messages": 42, "aircraft": []}),
        encoding="utf-8",
    )

    status = read_adsb_status(path)

    assert status["status"] == "online"
    assert status["messages"] == 42
    assert status["aircraft"] == 0


def test_adsb_status_reports_missing_json(tmp_path) -> None:
    status = read_adsb_status(tmp_path / "missing.json")

    assert status["status"] == "unavailable"


def test_diagnostics_package_omits_secrets() -> None:
    settings = Settings(
        admin_token="super-secret-token",
        radio_auto_detect=True,
    )
    payload = collect_diagnostics(
        settings=settings,
        health={"status": "ok", "live": True, "ready": True, "source_mode": "json"},
        gis={"version": 3, "last_good_version": 3, "load_ms": 12.5, "errors": []},
        coverage={"saved": True, "load_error": None, "save_error": None},
        host={"disk_free_gb": 10},
    )
    blob = json.dumps(payload)
    assert "super-secret-token" not in blob
    assert "stream_url" not in blob
    assert "user:pass" not in blob
    assert payload["settings"]["admin_required"] is True
    assert payload["app"]["version"] == "2.4.0"
    assert "caption" in payload["coverage"]
    assert payload["settings"]["acars_frequencies_mhz"] == [131.525, 131.550, 131.725, 131.825]
    assert "radio_channels" not in payload["settings"]
