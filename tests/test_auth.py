import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.broadcast import BroadcastHub
from app.config import Settings
from app.main import create_app


def _settings(tmp_path, **overrides) -> Settings:
    values = dict(
        layers_dir=tmp_path,
        readsb_json_path=tmp_path / "missing-aircraft.json",
        coverage_path=tmp_path / "coverage-rose.json",
        aircraft_types_path=tmp_path / "aircraft-types.json",
    )
    values.update(overrides)
    return Settings(**values)


def test_mutating_routes_require_admin_token(tmp_path) -> None:
    settings = _settings(tmp_path, admin_token="secret-token")
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/coverage/reset").status_code == 401
        assert client.post("/api/adsb/messages/clear").status_code == 401
        assert client.post("/api/adsb/raw/clear").status_code == 401
        created = client.post(
            "/api/aircraft-types",
            json={"icao": "abc123", "type_code": "A320"},
        )
        assert created.status_code == 401
        ok = client.post(
            "/api/coverage/reset",
            headers={"X-Admin-Token": "secret-token"},
        )
        assert ok.status_code == 200
        assert client.get("/api/aircraft").status_code == 200


def test_health_reports_live_and_task_readiness(tmp_path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        payload = client.get("/api/health").json()
        assert payload["live"] is True
        assert payload["ready"] is False
        assert payload["status"] == "degraded"
        assert payload["tasks"]["adsb-ingest"] == "running"
        assert payload["tasks"]["ws-broadcast"] == "running"
        assert payload["tasks"]["maintenance"] == "running"
        assert payload["adsb"]["status"] == "unavailable"


def test_config_exposes_admin_required_without_token(tmp_path) -> None:
    with TestClient(create_app(_settings(tmp_path, admin_token="secret"))) as client:
        payload = client.get("/api/config").json()
        assert payload["admin_required"] is True
        assert "secret" not in str(payload)


def test_websocket_rejects_foreign_origin(tmp_path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                "/ws/aircraft",
                headers={"Origin": "http://evil.example"},
            ):
                pass
        assert exc.value.code == 1008


def test_broadcast_hub_requires_positive_queue_size() -> None:
    with pytest.raises(ValueError):
        BroadcastHub(queue_size=0)
    with pytest.raises(ValueError):
        BroadcastHub(max_clients=0)
