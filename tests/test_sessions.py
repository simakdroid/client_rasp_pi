from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.gis import LayerManager
from app.main import create_app
from app.models import AircraftUpdate
from app.sessions import SessionRecorder
from app.tracker import AircraftTracker


@pytest.mark.asyncio
async def test_session_record_and_replay(tmp_path) -> None:
    recorder = SessionRecorder()
    recorder.start()
    recorder.record_batch(
        [
            AircraftUpdate(
                icao="abc123",
                lat=55.1,
                lon=37.1,
                received_at=datetime.now(UTC),
            )
        ]
    )
    recorder.stop()
    exported = recorder.export()
    assert exported["batches"] == 1
    assert exported["updates"] == 1

    layers = LayerManager(tmp_path)
    layers.refresh()
    tracker = AircraftTracker(55.0, 37.0, layers, 60, 10, 1)
    replayed = await recorder.replay(tracker)
    assert replayed["applied"] == 1
    snapshot = await tracker.snapshot()
    assert snapshot[0]["icao"] == "abc123"


def test_station_session_api(tmp_path) -> None:
    settings = Settings(
        layers_dir=tmp_path,
        readsb_json_path=tmp_path / "missing-aircraft.json",
        coverage_path=tmp_path / "coverage-rose.json",
        aircraft_types_path=tmp_path / "aircraft-types.json",
    )
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/station/session/start").json()["recording"] is True
        assert client.post("/api/station/session/stop").json()["recording"] is False
        exported = client.get("/api/station/session").json()
        assert exported["recording"] is False
        replay = client.post("/api/station/session/replay", json={})
        assert replay.status_code == 400
        loaded = client.post(
            "/api/station/session/replay",
            json={
                "events": [
                    {
                        "type": "batch",
                        "received_at": "2026-09-10T12:00:00+00:00",
                        "updates": [
                            {
                                "icao": "abc123",
                                "lat": 55.1,
                                "lon": 37.1,
                                "received_at": "2026-09-10T12:00:00+00:00",
                                "position_at": "2026-09-10T12:00:00+00:00",
                            }
                        ],
                    }
                ]
            },
        )
        assert loaded.status_code == 200
        assert loaded.json()["applied"] == 1
        assert client.get("/api/aircraft").json()["aircraft"][0]["icao"] == "abc123"
