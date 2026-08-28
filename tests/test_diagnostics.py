import json
import time

from app.diagnostics import read_adsb_status


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
