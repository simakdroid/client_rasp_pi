import asyncio

import pytest
from fastapi.testclient import TestClient

from app.broadcast import BroadcastHub
from app.config import Settings
from app.main import _abandon_tasks, create_app


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
            assert snapshot["generation"]
            assert snapshot["seq"] == 0
            assert snapshot["aircraft"] == []
            websocket.send_json({"type": "ping"})
            heartbeat = websocket.receive_json()
            assert heartbeat["type"] == "heartbeat"
            assert heartbeat["time"]


@pytest.mark.asyncio
async def test_slow_client_queue_is_replaced_with_resync() -> None:
    hub = BroadcastHub(queue_size=1, max_clients=2)
    queue = await hub.subscribe()
    await hub.publish({"type": "delta", "seq": 1})
    await hub.publish({"type": "delta", "seq": 2})
    assert queue.get_nowait() == {"type": "resync"}
    await hub.unsubscribe(queue)
    assert hub.client_count() == 0


@pytest.mark.asyncio
async def test_abandon_tasks_cancels_pending_work() -> None:
    started = asyncio.Event()

    async def hang() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(hang())
    await started.wait()
    await _abandon_tasks(task, None)
    assert task.done()
