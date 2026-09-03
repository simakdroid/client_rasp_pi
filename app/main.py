from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from .adsb import AdsbSource, ReadsbJsonSource, SbsSource
from .aircraft_types import AircraftTypeCatalog
from .broadcast import BroadcastHub
from .config import Settings, get_settings
from .models import AircraftTypeInput
from .diagnostics import read_adsb_status
from .gis import LayerManager
from .radio import RadioMonitor
from .raw_capture import RawCapture
from .raw_messages import RawMessageLog, ingest_raw_messages
from .sessions import SessionLog
from .tracker import AircraftTracker

LOGGER = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    layers = LayerManager(settings.layers_dir)
    type_catalog = AircraftTypeCatalog(settings.aircraft_types_path)
    session_log = SessionLog(settings.sessions_dir, settings.sessions_keep_days)
    tracker = AircraftTracker(
        settings.station_lat,
        settings.station_lon,
        layers,
        settings.aircraft_ttl_s,
        settings.track_max_points,
        settings.track_min_distance_m,
        settings.event_log_size,
        settings.archive_max_aircraft,
        settings.coverage_path,
        settings.coverage_max_km,
        type_catalog,
        session_log,
    )
    hub = BroadcastHub()
    raw_capture = RawCapture(
        settings.raw_capture_dir, settings.raw_capture_keep_days
    )
    raw_messages = RawMessageLog(settings.raw_log_size, raw_capture)
    radio = RadioMonitor(
        settings.radio_channels,
        settings.radio_stats_path,
        auto_detect=settings.radio_auto_detect,
        min_receivers=settings.radio_min_rtl_receivers,
        receiver_serial=settings.radio_receiver_serial,
        sysfs_path=settings.usb_sysfs_path,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        layers.refresh()
        source = _create_source(settings)
        tasks = [
            asyncio.create_task(_ingest(source, tracker), name="adsb-ingest"),
            asyncio.create_task(
                ingest_raw_messages(
                    settings.raw_host, settings.raw_port, raw_messages
                ),
                name="adsb-raw-ingest",
            ),
            asyncio.create_task(
                _broadcast_loop(tracker, hub, settings.websocket_interval_s),
                name="ws-broadcast",
            ),
            asyncio.create_task(_maintenance_loop(tracker, layers), name="maintenance"),
        ]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await tracker.flush_coverage()
            await tracker.flush_sessions()

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.layers = layers
    app.state.tracker = tracker
    app.state.hub = hub
    app.state.raw_messages = raw_messages
    app.state.raw_capture = raw_capture
    app.state.type_catalog = type_catalog
    app.state.session_log = session_log

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["*"],
        )

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "time": datetime.now(UTC).isoformat(),
            "adsb": await asyncio.to_thread(
                read_adsb_status, settings.readsb_json_path
            ),
            "raw_capture": raw_capture.status(),
        }

    @app.get("/api/config")
    async def public_config() -> dict[str, object]:
        return {
            "station": {
                "name": settings.station_name,
                "lat": settings.station_lat,
                "lon": settings.station_lon,
            },
            "map": {
                "osm": {"url": settings.osm_url, "max_zoom": 19},
                "openflightmaps_url": settings.ofm_url,
                "zoom": 8,
            },
            "radio": {
                "enabled": await asyncio.to_thread(radio.hardware_available)
                if settings.radio_auto_detect
                else bool(settings.radio_channels)
            },
            "websocket_interval_ms": round(settings.websocket_interval_s * 1000),
            "sessions": {"keep_days": settings.sessions_keep_days},
            "raw_capture": {
                "enabled": settings.raw_capture_dir is not None,
                "keep_days": settings.raw_capture_keep_days,
            },
        }

    @app.get("/api/coverage")
    async def coverage_rose() -> dict[str, object]:
        return await tracker.coverage_snapshot()

    @app.post("/api/coverage/reset")
    async def reset_coverage_rose() -> dict[str, object]:
        return await tracker.reset_coverage()

    @app.get("/api/aircraft-types")
    async def aircraft_types() -> dict[str, object]:
        return {"types": type_catalog.list()}

    @app.post("/api/aircraft-types")
    async def upsert_aircraft_type(body: AircraftTypeInput) -> dict[str, object]:
        try:
            entry = type_catalog.upsert(body.icao, body.type_code, body.type_desc)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await tracker.mark_type_changed(entry["icao"])
        return entry

    @app.delete("/api/aircraft-types/{icao}")
    async def delete_aircraft_type(icao: str) -> dict[str, object]:
        try:
            deleted = type_catalog.delete(icao)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="Aircraft type not found")
        await tracker.mark_type_changed(icao)
        return {"ok": True}

    @app.get("/api/sessions")
    async def sessions() -> dict[str, object]:
        return {
            "keep_days": settings.sessions_keep_days,
            "sessions": await asyncio.to_thread(session_log.list),
        }

    @app.get("/api/aircraft")
    async def aircraft() -> dict[str, object]:
        return {
            "type": "snapshot",
            "timestamp": datetime.now(UTC).isoformat(),
            "aircraft": await tracker.snapshot(),
            "archived": await tracker.archived_snapshot(),
        }

    @app.get("/api/adsb/messages")
    async def adsb_messages(
        after_id: int = Query(default=0, ge=0),
        before_id: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        newest_first: bool = Query(default=False),
    ) -> dict[str, object]:
        return await tracker.recent_events(
            after_id=after_id,
            before_id=before_id,
            limit=limit,
            newest_first=newest_first,
        )

    @app.post("/api/adsb/messages/clear")
    async def clear_adsb_messages() -> dict[str, object]:
        return await tracker.clear_events()

    @app.get("/api/adsb/raw")
    async def adsb_raw_messages(
        after_id: int = Query(default=0, ge=0),
        before_id: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        newest_first: bool = Query(default=False),
    ) -> dict[str, object]:
        payload = await raw_messages.recent(
            after_id=after_id,
            before_id=before_id,
            limit=limit,
            newest_first=newest_first,
        )
        payload["messages"] = await tracker.attach_mode_s_context(payload["messages"])
        return payload

    @app.post("/api/adsb/raw/clear")
    async def clear_adsb_raw_messages() -> dict[str, object]:
        return await raw_messages.clear()

    @app.get("/api/layers")
    async def layer_list() -> list[dict[str, object]]:
        return layers.list_layers()

    @app.get("/api/layers/{layer_id}")
    async def vector_layer(layer_id: str) -> dict[str, object]:
        try:
            return layers.get_vector(layer_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Layer not found") from exc

    @app.get("/api/tiles/{layer_id}/{z}/{x}/{y}")
    async def map_tile(layer_id: str, z: int, x: int, y: int) -> Response:
        if not (0 <= z <= 24 and 0 <= x < 2**z and 0 <= y < 2**z):
            raise HTTPException(status_code=400, detail="Invalid tile coordinates")
        try:
            tile, tile_format = await asyncio.gather(
                asyncio.to_thread(layers.get_tile, layer_id, z, x, y),
                asyncio.to_thread(layers.tile_format, layer_id),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Layer not found") from exc
        if tile is None:
            raise HTTPException(status_code=404, detail="Tile not found")
        return Response(
            tile,
            media_type={
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "webp": "image/webp",
            }.get(tile_format, "application/vnd.mapbox-vector-tile"),
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.get("/api/radio/channels")
    async def radio_channels() -> list[dict[str, object]]:
        return await radio.status()

    @app.websocket("/ws/aircraft")
    async def aircraft_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = await hub.subscribe()
        try:
            await websocket.send_json(
                {
                    "type": "snapshot",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "aircraft": await tracker.snapshot(),
                    "archived": await tracker.archived_snapshot(),
                }
            )
            while True:
                await websocket.send_json(await queue.get())
        except WebSocketDisconnect:
            pass
        finally:
            await hub.unsubscribe(queue)

    app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="ui")
    return app


def _create_source(settings: Settings) -> AdsbSource:
    if settings.adsb_source == "sbs":
        return SbsSource(settings.sbs_host, settings.sbs_port)
    return ReadsbJsonSource(
        settings.readsb_json_path,
        settings.adsb_poll_interval_s,
        settings.aircraft_ttl_s,
    )


async def _ingest(source: AdsbSource, tracker: AircraftTracker) -> None:
    async for batch in source.updates():
        await tracker.apply(batch)


async def _broadcast_loop(
    tracker: AircraftTracker, hub: BroadcastHub, interval_s: float
) -> None:
    while True:
        await asyncio.sleep(interval_s)
        if delta := await tracker.consume_delta():
            await hub.publish(delta)


async def _maintenance_loop(tracker: AircraftTracker, layers: LayerManager) -> None:
    layer_refresh_tick = 0
    while True:
        await asyncio.sleep(5)
        await tracker.prune()
        await tracker.refresh_manual_types()
        layer_refresh_tick += 1
        if layer_refresh_tick >= 6:
            layer_refresh_tick = 0
            with contextlib.suppress(OSError, ValueError):
                await asyncio.to_thread(layers.refresh)
            await tracker.flush_coverage()


app = create_app()
