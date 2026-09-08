from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_websocket_sends_heartbeat_when_idle(tmp_path) -> None:
    settings = Settings(
        layers_dir=tmp_path,
        readsb_json_path=tmp_path / "missing-aircraft.json",
        coverage_path=tmp_path / "coverage-rose.json",
        aircraft_types_path=tmp_path / "aircraft-types.json",
        websocket_heartbeat_s=1,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/config").json()["websocket_heartbeat_ms"] == 1000
        with client.websocket_connect("/ws/aircraft") as websocket:
            snapshot = websocket.receive_json()
            assert snapshot["type"] == "snapshot"
            websocket.send_json({"type": "ping"})
            heartbeat = websocket.receive_json()
            assert heartbeat["type"] == "heartbeat"
            assert heartbeat["time"]
