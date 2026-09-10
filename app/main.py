from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .adsb import AdsbSource, ReadsbJsonSource, SbsSource
from .aircraft_types import AircraftTypeCatalog
from .auth import log_insecure_admin_config, require_admin, websocket_origin_allowed
from .broadcast import BroadcastHub, ClientLimitError
from .config import Settings, get_settings
from .diagnostics import collect_diagnostics, read_adsb_status, read_host_status
from .gis import LayerManager, UnsupportedTileFormatError, tile_http_metadata
from .models import AircraftTypeInput
from .radio import RadioMonitor, rewrite_loopback_stream_url
from .raw_messages import RawMessageLog, ingest_raw_messages
from .runtime import RuntimeStatus, run_supervised
from .sessions import SessionRecorder, SessionReplayInput
from .tracker import GEOFENCE_LEAVE_MISSES, AircraftTracker

LOGGER = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    layers = LayerManager(settings.layers_dir)
    type_catalog = AircraftTypeCatalog(settings.aircraft_types_path)
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
        max_active_aircraft=settings.max_active_aircraft,
    )
    hub = BroadcastHub(
        queue_size=settings.websocket_queue_size,
        max_clients=settings.websocket_max_clients,
    )
    raw_messages = RawMessageLog(settings.raw_log_size)
    radio = RadioMonitor(
        settings.radio_channels,
        settings.radio_stats_path,
        auto_detect=settings.radio_auto_detect,
        min_receivers=settings.radio_min_rtl_receivers,
        receiver_serial=settings.radio_receiver_serial,
        sysfs_path=settings.usb_sysfs_path,
    )
    runtime = RuntimeStatus()
    recorder = SessionRecorder()
    source = _create_source(settings)
    tile_io = asyncio.Semaphore(4)
    log_insecure_admin_config(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        layers.refresh()
        tasks = [
            asyncio.create_task(
                run_supervised(
                    "adsb-ingest",
                    lambda: _ingest(source, tracker, runtime, recorder),
                    runtime,
                ),
                name="adsb-ingest",
            ),
            asyncio.create_task(
                run_supervised(
                    "adsb-raw-ingest",
                    lambda: ingest_raw_messages(
                        settings.raw_host, settings.raw_port, raw_messages
                    ),
                    runtime,
                    fatal=False,
                ),
                name="adsb-raw-ingest",
            ),
            asyncio.create_task(
                run_supervised(
                    "ws-broadcast",
                    lambda: _broadcast_loop(tracker, hub, settings.websocket_interval_s),
                    runtime,
                ),
                name="ws-broadcast",
            ),
            asyncio.create_task(
                run_supervised(
                    "maintenance",
                    lambda: _maintenance_loop(tracker, layers),
                    runtime,
                ),
                name="maintenance",
            ),
        ]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await tracker.flush_coverage()

    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.layers = layers
    app.state.tracker = tracker
    app.state.hub = hub
    app.state.raw_messages = raw_messages
    app.state.type_catalog = type_catalog
    app.state.runtime = runtime
    app.state.source = source
    app.state.recorder = recorder

    if settings.trusted_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["*"],
        )

    async def _ops_snapshot() -> dict[str, object]:
        adsb = await asyncio.to_thread(read_adsb_status, settings.readsb_json_path)
        runtime.set_source(**source.health())
        health = runtime.snapshot(adsb=adsb, source_mode=settings.adsb_source)
        coverage = await tracker.coverage_snapshot()
        catalog = layers.catalog_payload()
        host = await asyncio.to_thread(read_host_status, settings.coverage_path.parent)
        positions = await tracker.position_stats()
        roles = radio.role_snapshot(settings.adsb_preferred_serial)
        quality = await radio.quality_snapshot()
        return {
            "health": health,
            "coverage": coverage,
            "catalog": catalog,
            "host": host,
            "positions": positions,
            "radio": {
                **roles,
                "receiver_serial": settings.radio_receiver_serial,
                "preferred_adsb_serial": settings.adsb_preferred_serial,
                "enabled": roles["vhf_available"]
                if settings.radio_auto_detect
                else bool(settings.radio_channels),
                "active_channels": sum(1 for item in quality if item.get("active") is True),
                "channels": quality,
            },
            "session": recorder.status(),
        }

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        adsb = await asyncio.to_thread(read_adsb_status, settings.readsb_json_path)
        runtime.set_source(**source.health())
        return runtime.snapshot(adsb=adsb, source_mode=settings.adsb_source)

    @app.get("/api/station")
    async def station_status() -> dict[str, object]:
        ops = await _ops_snapshot()
        health = ops["health"]
        catalog = ops["catalog"]
        coverage = ops["coverage"]
        assert isinstance(health, dict)
        assert isinstance(catalog, dict)
        assert isinstance(coverage, dict)
        return {
            **health,
            "source_mode": settings.adsb_source,
            "station": {
                "name": settings.station_name,
                "lat": settings.station_lat,
                "lon": settings.station_lon,
            },
            "host": ops["host"],
            "positions": ops["positions"],
            "gis": {
                "version": catalog["version"],
                "last_good_version": catalog["last_good_version"],
                "loaded_at": catalog["loaded_at"],
                "load_ms": catalog["load_ms"],
                "feature_count": catalog["feature_count"],
                "layer_count": len(catalog["layers"]),
                "geofence_count": catalog["geofence_count"],
                "error_count": len(catalog["errors"]),
                "errors": catalog["errors"],
            },
            "coverage": {
                "kind": coverage.get("kind"),
                "caption": coverage.get("caption"),
                "saved": coverage.get("saved"),
                "save_error": coverage.get("save_error"),
                "load_error": coverage.get("load_error"),
                "filled_bins": coverage.get("filled_bins"),
                "max_range_km": coverage.get("max_range_km"),
                "observations": coverage.get("observations"),
                "range_updates": coverage.get("range_updates"),
                "altitude_bands": coverage.get("altitude_bands"),
                "hourly": coverage.get("hourly"),
            },
            "radio": ops["radio"],
            "session": ops["session"],
        }

    @app.post("/api/station/session/start", dependencies=[Depends(require_admin)])
    async def start_session_recording() -> dict[str, object]:
        return recorder.start()

    @app.post("/api/station/session/stop", dependencies=[Depends(require_admin)])
    async def stop_session_recording() -> dict[str, object]:
        return recorder.stop()

    @app.get("/api/station/session")
    async def session_recording() -> dict[str, object]:
        return recorder.export()

    @app.post("/api/station/session/replay", dependencies=[Depends(require_admin)])
    async def replay_session_recording(
        body: SessionReplayInput | None = None,
    ) -> dict[str, object]:
        payload = body or SessionReplayInput()
        if payload.events:
            try:
                recorder.load(payload.model_dump())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if recorder.status()["events"] == 0:
            raise HTTPException(status_code=400, detail="No session events to replay")
        return await recorder.replay(tracker)

    @app.get("/api/station/diagnostics")
    async def station_diagnostics() -> dict[str, object]:
        ops = await _ops_snapshot()
        health = ops["health"]
        catalog = ops["catalog"]
        assert isinstance(health, dict)
        assert isinstance(catalog, dict)
        return collect_diagnostics(
            settings=settings,
            health={**health, "source_mode": settings.adsb_source},
            gis={
                **catalog,
                "layer_count": len(catalog["layers"]),
            },
            coverage=ops["coverage"],
            host=ops["host"],
            radio=ops["radio"],
            positions=ops["positions"],
            session=ops["session"],
        )

    @app.get("/api/geofence/events")
    async def geofence_events(
        after_id: int = Query(default=0, ge=0),
        before_id: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        newest_first: bool = Query(default=False),
    ) -> dict[str, object]:
        payload = await tracker.recent_events(
            after_id=after_id,
            before_id=before_id,
            limit=limit,
            newest_first=newest_first,
            kinds=("geofence_enter", "geofence_leave"),
        )
        payload["hysteresis_leave_after"] = GEOFENCE_LEAVE_MISSES
        return payload


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
            "admin_required": bool(settings.admin_token),
            "track_max_points": settings.track_max_points,
            "websocket_interval_ms": round(settings.websocket_interval_s * 1000),
            "websocket_heartbeat_ms": round(settings.websocket_heartbeat_s * 1000),
        }

    @app.get("/api/coverage")
    async def coverage_rose() -> dict[str, object]:
        return await tracker.coverage_snapshot()

    @app.post("/api/coverage/reset", dependencies=[Depends(require_admin)])
    async def reset_coverage_rose() -> dict[str, object]:
        snapshot = await tracker.reset_coverage()
        if snapshot.get("save_error"):
            raise HTTPException(status_code=503, detail=str(snapshot["save_error"]))
        return snapshot


    @app.get("/api/aircraft-types")
    async def aircraft_types() -> dict[str, object]:
        return {
            "types": type_catalog.list(),
            "version": type_catalog.version,
        }

    @app.post("/api/aircraft-types", dependencies=[Depends(require_admin)])
    async def upsert_aircraft_type(body: AircraftTypeInput) -> dict[str, object]:
        try:
            entry = await tracker.persist_type_upsert(body.icao, body.type_code, body.type_desc)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        await tracker.mark_type_changed(entry["icao"])
        return entry

    @app.delete("/api/aircraft-types/{icao}", dependencies=[Depends(require_admin)])
    async def delete_aircraft_type(icao: str) -> dict[str, object]:
        try:
            deleted = await tracker.persist_type_delete(icao)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="Aircraft type not found")
        await tracker.mark_type_changed(icao)
        return {"ok": True}

    @app.get("/api/aircraft")
    async def aircraft() -> dict[str, object]:
        return await tracker.snapshot_message()

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

    @app.post("/api/adsb/messages/clear", dependencies=[Depends(require_admin)])
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

    @app.post("/api/adsb/raw/clear", dependencies=[Depends(require_admin)])
    async def clear_adsb_raw_messages() -> dict[str, object]:
        return await raw_messages.clear()

    @app.get("/api/layers")
    async def layer_list() -> dict[str, object]:
        return layers.catalog_payload()

    @app.get("/api/layers/{layer_id}")
    async def vector_layer(layer_id: str) -> dict[str, object]:
        try:
            return layers.get_vector(layer_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Layer not found") from exc

    @app.get("/api/tiles/{layer_id}/{z}/{x}/{y}")
    async def map_tile(
        layer_id: str, z: int, x: int, y: int, request: Request
    ) -> Response:
        if not (0 <= z <= 24 and 0 <= x < 2**z and 0 <= y < 2**z):
            raise HTTPException(status_code=400, detail="Invalid tile coordinates")
        catalog = layers.catalog
        layer = catalog.layers.get(layer_id)
        if layer is None or layer.get("kind") != "mbtiles":
            raise HTTPException(status_code=404, detail="Layer not found")
        etag = f'"{layer_id}-{catalog.version}-{z}-{x}-{y}"'
        cache_headers = {
            "ETag": etag,
            "Cache-Control": "public, max-age=120, must-revalidate",
        }
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=cache_headers)
        try:
            async with tile_io:
                tile, tile_format, tile_version = await asyncio.to_thread(
                    layers.tile_payload, layer_id, z, x, y
                )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Layer not found") from exc
        etag = f'"{layer_id}-{tile_version}-{z}-{x}-{y}"'
        cache_headers["ETag"] = etag
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=cache_headers)
        if tile is None:
            raise HTTPException(status_code=404, detail="Tile not found")
        try:
            media_type, encoding_headers = tile_http_metadata(tile, tile_format)
        except UnsupportedTileFormatError as exc:
            raise HTTPException(
                status_code=415, detail=f"Unsupported tile format: {exc}"
            ) from exc
        return Response(
            tile,
            media_type=media_type,
            headers={**cache_headers, **encoding_headers},
        )

    @app.get("/api/radio/channels")
    async def radio_channels(request: Request) -> list[dict[str, object]]:
        channels = await radio.status()
        host = request.url.hostname or ""
        for item in channels:
            stream_url = item.get("stream_url")
            if isinstance(stream_url, str):
                item["stream_url"] = rewrite_loopback_stream_url(stream_url, host)
        return channels

    @app.websocket("/ws/aircraft")
    async def aircraft_socket(websocket: WebSocket) -> None:
        if not websocket_origin_allowed(websocket, settings):
            await websocket.close(code=1008, reason="Origin not allowed")
            return
        await websocket.accept()
        try:
            queue = await hub.subscribe()
        except ClientLimitError:
            await websocket.close(code=1013, reason="Too many connections")
            return
        stop = asyncio.Event()

        async def drain_client() -> None:
            try:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        break
            except WebSocketDisconnect:
                pass
            except Exception:
                LOGGER.debug("WebSocket receive task ended unexpectedly", exc_info=True)
            finally:
                stop.set()

        drain = asyncio.create_task(drain_client())
        try:
            await _send_json(
                websocket,
                await tracker.snapshot_message(),
                settings.websocket_send_timeout_s,
            )
            await _websocket_keepalive(
                websocket,
                queue,
                stop,
                settings.websocket_heartbeat_s,
                settings.websocket_send_timeout_s,
            )
        except WebSocketDisconnect:
            pass
        except TimeoutError:
            with contextlib.suppress(Exception):
                await websocket.close(code=1001, reason="Send timeout")
        except Exception:
            LOGGER.debug("WebSocket session ended unexpectedly", exc_info=True)
        finally:
            stop.set()
            drain.cancel()
            try:
                with contextlib.suppress(asyncio.CancelledError):
                    await drain
            finally:
                await hub.unsubscribe(queue)

    app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="ui")
    return app


def _create_source(settings: Settings) -> AdsbSource:
    if settings.adsb_source == "sbs":
        return SbsSource(settings.sbs_host, settings.sbs_port, settings.sbs_timezone)
    return ReadsbJsonSource(
        settings.readsb_json_path,
        settings.adsb_poll_interval_s,
        settings.aircraft_ttl_s,
    )


async def _ingest(
    source: AdsbSource,
    tracker: AircraftTracker,
    runtime: RuntimeStatus,
    recorder: SessionRecorder,
) -> None:
    async for batch in source.updates():
        runtime.set_source(**source.health())
        if batch:
            runtime.note_batch()
            await tracker.apply(batch)
            recorder.record_batch(batch)


async def _send_json(websocket: WebSocket, payload: dict[str, object], timeout_s: float) -> None:
    await asyncio.wait_for(websocket.send_json(payload), timeout=timeout_s)


async def _abandon_tasks(*tasks: asyncio.Task[object] | None) -> None:
    running = [task for task in tasks if task is not None and not task.done()]
    for task in running:
        task.cancel()
    for task in running:
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _websocket_keepalive(
    websocket: WebSocket,
    queue: asyncio.Queue[dict[str, object]],
    stop: asyncio.Event,
    heartbeat_s: float,
    send_timeout_s: float,
) -> None:
    """Send aircraft deltas, or a heartbeat when the socket would otherwise go idle."""
    get_task: asyncio.Task[dict[str, object]] | None = None
    stop_task: asyncio.Task[bool] | None = None
    try:
        while not stop.is_set():
            get_task = asyncio.create_task(queue.get())
            stop_task = asyncio.create_task(stop.wait())
            try:
                done, pending = await asyncio.wait(
                    {get_task, stop_task},
                    timeout=heartbeat_s,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                await _abandon_tasks(*pending)
                if stop.is_set():
                    return
                if get_task in done and not get_task.cancelled():
                    await _send_json(websocket, get_task.result(), send_timeout_s)
                    continue
                await _send_json(
                    websocket,
                    {"type": "heartbeat", "time": datetime.now(UTC).isoformat()},
                    send_timeout_s,
                )
            finally:
                await _abandon_tasks(get_task, stop_task)
                get_task = None
                stop_task = None
    finally:
        await _abandon_tasks(get_task, stop_task)


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
            try:
                changed = await asyncio.to_thread(layers.refresh)
            except Exception:
                LOGGER.warning("GIS catalog refresh failed", exc_info=True)
            else:
                if changed:
                    await tracker.refresh_geofences()
            await tracker.flush_coverage()


app = create_app()
