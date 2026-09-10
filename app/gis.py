from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from defusedxml import ElementTree
from shapely import STRtree
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.prepared import PreparedGeometry, prep

SUPPORTED_VECTOR_SUFFIXES = {".geojson", ".json", ".kml"}
LOGGER = logging.getLogger(__name__)
MAX_FEATURES = 20_000
MAX_POSITIONS = 200_000
KML_NS = {"k": "http://www.opengis.net/kml/2.2"}
TILE_MEDIA_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "pbf": "application/vnd.mapbox-vector-tile",
    "mvt": "application/vnd.mapbox-vector-tile",
}
GZIP_MAGIC = b"\x1f\x8b"
LAYER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
GEOFENCE_POLICY = {
    "boundary": "inclusive",
    "control_tiebreak": "higher_priority, then smaller_area, then layer_id:name",
}


class UnsupportedTileFormatError(ValueError):
    """MBTiles declared a raster/vector format this server will not serve."""


def tile_http_metadata(tile: bytes, tile_format: str) -> tuple[str, dict[str, str]]:
    """Return Content-Type and extra headers without decompressing gzip tiles."""
    fmt = str(tile_format or "").strip().lower()
    media_type = TILE_MEDIA_TYPES.get(fmt)
    if media_type is None:
        raise UnsupportedTileFormatError(fmt or "unknown")
    headers: dict[str, str] = {}
    if tile.startswith(GZIP_MAGIC):
        headers["Content-Encoding"] = "gzip"
    return media_type, headers


@dataclass(slots=True)
class Geofence:
    layer_id: str
    name: str
    geometry: PreparedGeometry
    shape: BaseGeometry
    min_alt_ft: float | None = None
    max_alt_ft: float | None = None
    min_alt_exclusive: bool = False
    code: str | None = None
    priority: int = 0

    @property
    def key(self) -> str:
        return f"{self.layer_id}:{self.name}"

    def contains(self, lon: float, lat: float, altitude_ft: int | None) -> bool:
        if self.min_alt_ft is not None:
            if altitude_ft is None:
                return False
            if self.min_alt_exclusive:
                if altitude_ft <= self.min_alt_ft:
                    return False
            elif altitude_ft < self.min_alt_ft:
                return False
        if self.max_alt_ft is not None and (
            altitude_ft is None or altitude_ft > self.max_alt_ft
        ):
            return False
        return self.geometry.covers(Point(lon, lat))


def _control_better(candidate: Geofence, current: Geofence) -> bool:
    """Pick the more specific control zone when several codes overlap."""
    if candidate.priority != current.priority:
        return candidate.priority > current.priority
    area_c = candidate.shape.area
    area_n = current.shape.area
    if area_c != area_n:
        return area_c < area_n
    return candidate.key < current.key


@dataclass(slots=True)
class LayerCatalog:
    """Immutable GIS generation: metadata, parsed vectors and geofences together."""

    version: int
    layers: dict[str, dict[str, Any]] = field(default_factory=dict)
    files: dict[str, Path] = field(default_factory=dict)
    vectors: dict[str, dict[str, Any]] = field(default_factory=dict)
    geofences: tuple[Geofence, ...] = ()
    geofence_tree: STRtree | None = None
    errors: tuple[dict[str, str], ...] = ()
    fingerprints: dict[str, tuple[int, int]] = field(default_factory=dict)
    loaded_at: datetime | None = None
    load_ms: float = 0.0
    last_good_version: int = 0
    feature_count: int = 0


class LayerManager:
    """Loads trusted, administrator-provided GIS files from one directory."""

    def __init__(self, layers_dir: Path) -> None:
        self.layers_dir = layers_dir
        self._catalog = LayerCatalog(version=0)
        self._last_good_version = 0

    @property
    def catalog(self) -> LayerCatalog:
        return self._catalog

    def refresh(self) -> bool:
        started = time.perf_counter()
        self.layers_dir.mkdir(parents=True, exist_ok=True)
        previous = self._catalog
        layers: dict[str, dict[str, Any]] = {}
        files: dict[str, Path] = {}
        vectors: dict[str, dict[str, Any]] = {}
        geofences: list[Geofence] = []
        errors: list[dict[str, str]] = []
        fingerprints: dict[str, tuple[int, int]] = {}

        for path in sorted(self.layers_dir.iterdir()):
            suffix = path.suffix.lower()
            if not path.is_file() or suffix not in SUPPORTED_VECTOR_SUFFIXES | {".mbtiles"}:
                continue
            layer_id = path.stem
            if not LAYER_ID_RE.fullmatch(layer_id):
                errors.append({
                    "id": layer_id,
                    "error": "invalid layer id",
                    "reason": "invalid layer id",
                })
                continue
            if layer_id in layers:
                errors.append({
                    "id": layer_id,
                    "error": "duplicate layer id",
                    "reason": "duplicate layer id",
                })
                continue
            fingerprint = _file_fingerprint(path)
            if (
                fingerprint is not None
                and previous.fingerprints.get(layer_id) == fingerprint
                and layer_id in previous.layers
            ):
                _keep_layer(previous, layer_id, layers, files, vectors, geofences, fingerprints)
                continue
            try:
                if suffix == ".mbtiles":
                    record = self._load_mbtiles(path, layer_id)
                    layers[layer_id] = record
                    files[layer_id] = path
                    fingerprints[layer_id] = fingerprint or (0, 0)
                    continue
                data = self._load_vector(path)
                layers[layer_id] = {
                    "id": layer_id,
                    "name": str(data.get("name") or layer_id),
                    "kind": "geojson",
                    "feature_count": len(data.get("features", [])),
                }
                files[layer_id] = path
                vectors[layer_id] = data
                fingerprints[layer_id] = fingerprint or (0, 0)
                self._append_geofences(layer_id, data, geofences)
            except Exception as exc:
                LOGGER.warning("Skipping invalid GIS layer %s: %s", path, exc)
                if layer_id in previous.layers:
                    _keep_layer(
                        previous, layer_id, layers, files, vectors, geofences, fingerprints
                    )
                    errors.append(
                        {
                            "id": layer_id,
                            "error": str(exc),
                            "reason": str(exc),
                            "kept_previous": True,
                        }
                    )
                else:
                    errors.append(
                        {
                            "id": layer_id,
                            "error": str(exc),
                            "reason": str(exc),
                            "kept_previous": False,
                        }
                    )

        version = previous.version
        changed = (
            layers != previous.layers
            or files != previous.files
            or fingerprints != previous.fingerprints
            or errors != list(previous.errors)
        )
        if changed:
            version += 1
        geofence_tuple = tuple(geofences)
        if layers:
            self._last_good_version = version
        feature_count = sum(
            int(item.get("feature_count") or 0) for item in layers.values()
        )
        catalog = LayerCatalog(
            version=version,
            layers=layers,
            files=files,
            vectors=vectors,
            geofences=geofence_tuple,
            geofence_tree=_geofence_tree(geofence_tuple),
            errors=tuple(errors),
            fingerprints=fingerprints,
            loaded_at=datetime.now(UTC),
            load_ms=round((time.perf_counter() - started) * 1000, 1),
            last_good_version=self._last_good_version,
            feature_count=feature_count,
        )
        self._catalog = catalog
        return changed

    def list_layers(self) -> list[dict[str, Any]]:
        catalog = self._catalog
        result: list[dict[str, Any]] = []
        for layer in catalog.layers.values():
            item = dict(layer)
            item["catalog_version"] = catalog.version
            if item.get("kind") == "mbtiles":
                item["tile_url"] = f"/api/tiles/{item['id']}/{{z}}/{{x}}/{{y}}?v={catalog.version}"
            result.append(item)
        return result

    def catalog_payload(self) -> dict[str, Any]:
        catalog = self._catalog
        return {
            "version": catalog.version,
            "loaded_at": catalog.loaded_at.isoformat() if catalog.loaded_at else None,
            "load_ms": catalog.load_ms,
            "last_good_version": catalog.last_good_version,
            "feature_count": catalog.feature_count,
            "geofence_count": len(catalog.geofences),
            "geofence_policy": dict(GEOFENCE_POLICY),
            "layers": self.list_layers(),
            "errors": [_public_gis_error(item) for item in catalog.errors],
        }

    def get_vector(self, layer_id: str) -> dict[str, Any]:
        catalog = self._catalog
        layer = catalog.layers.get(layer_id)
        vector = catalog.vectors.get(layer_id)
        if layer is None or vector is None or layer.get("kind") != "geojson":
            raise KeyError(layer_id)
        return {
            "id": layer_id,
            "version": catalog.version,
            "name": layer["name"],
            "geojson": vector,
        }

    def matching_geofences(
        self, lon: float, lat: float, altitude_ft: int | None
    ) -> set[str]:
        names, _sector, _matched = self.match_airspace(lon, lat, altitude_ft)
        return names

    def matching_control_code(
        self, lon: float, lat: float, altitude_ft: int | None
    ) -> str | None:
        _names, sector, _matched = self.match_airspace(lon, lat, altitude_ft)
        return sector

    def match_airspace(
        self, lon: float, lat: float, altitude_ft: int | None
    ) -> tuple[set[str], str | None, tuple[Geofence, ...]]:
        catalog = self._catalog
        if not catalog.geofences:
            return set(), None, ()
        point = Point(lon, lat)
        if catalog.geofence_tree is None:
            candidates: tuple[Geofence, ...] | list[Geofence] = catalog.geofences
        else:
            candidates = [
                catalog.geofences[int(index)]
                for index in catalog.geofence_tree.query(point)
            ]
        names: set[str] = set()
        matched: list[Geofence] = []
        best: Geofence | None = None
        for geofence in candidates:
            if not geofence.contains(lon, lat, altitude_ft):
                continue
            names.add(geofence.name)
            matched.append(geofence)
            if geofence.code and (best is None or _control_better(geofence, best)):
                best = geofence
        return names, None if best is None else best.code, tuple(matched)

    def get_tile(self, layer_id: str, z: int, x: int, y_xyz: int) -> bytes | None:
        tile, _fmt, _version = self.tile_payload(layer_id, z, x, y_xyz)
        return tile

    def tile_format(self, layer_id: str) -> str:
        catalog = self._catalog
        layer = catalog.layers.get(layer_id)
        if layer is None or layer.get("kind") != "mbtiles":
            raise KeyError(layer_id)
        return str(layer["format"])

    def tile_payload(
        self, layer_id: str, z: int, x: int, y_xyz: int
    ) -> tuple[bytes | None, str, int]:
        catalog = self._catalog
        layer = catalog.layers.get(layer_id)
        path = catalog.files.get(layer_id)
        if layer is None or path is None or layer.get("kind") != "mbtiles":
            raise KeyError(layer_id)
        y_tms = (1 << z) - 1 - y_xyz
        with closing(_sqlite_open(path)) as connection:
            row = connection.execute(
                "SELECT tile_data FROM tiles "
                "WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
                (z, x, y_tms),
            ).fetchone()
        return (bytes(row[0]) if row else None, str(layer["format"]), catalog.version)

    @staticmethod
    def _load_mbtiles(path: Path, layer_id: str) -> dict[str, Any]:
        metadata = _mbtiles_metadata(path)
        tile_format = str(metadata.get("format", "pbf")).strip().lower() or "pbf"
        if tile_format not in TILE_MEDIA_TYPES:
            raise ValueError(f"unsupported MBTiles format {tile_format!r}")
        vector_layers: list[str] = []
        if metadata.get("json"):
            try:
                vector_layers = [
                    str(item["id"])
                    for item in json.loads(metadata["json"]).get("vector_layers", [])
                    if item.get("id")
                ]
            except (json.JSONDecodeError, TypeError, AttributeError):
                LOGGER.warning("Invalid vector layer metadata in %s", path)
        return {
            "id": layer_id,
            "name": metadata.get("name", layer_id),
            "kind": "mbtiles",
            "format": tile_format,
            "tile_url": f"/api/tiles/{layer_id}/{{z}}/{{x}}/{{y}}",
            "minzoom": _optional_float(metadata.get("minzoom")),
            "maxzoom": _optional_float(metadata.get("maxzoom")),
            "vector_layers": vector_layers,
        }

    @staticmethod
    def _load_vector(path: Path) -> dict[str, Any]:
        if path.suffix.lower() == ".kml":
            data = LayerManager._kml_to_geojson(path)
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
        return _normalize_feature_collection(data, path)

    @staticmethod
    def _append_geofences(
        layer_id: str, data: dict[str, Any], target: list[Geofence]
    ) -> None:
        for index, feature in enumerate(data.get("features", [])):
            if not isinstance(feature, dict):
                continue
            geometry_data = feature.get("geometry")
            if not geometry_data:
                continue
            try:
                geometry = shape(geometry_data)
            except Exception as exc:
                LOGGER.warning("Skipping invalid geometry in %s: %s", layer_id, exc)
                continue
            if geometry.geom_type not in {"Polygon", "MultiPolygon"} or geometry.is_empty:
                continue
            if not geometry.is_valid:
                LOGGER.warning("Skipping invalid polygon %s:%s", layer_id, index + 1)
                continue
            props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
            try:
                if _optional_bool(props.get("geofence"), True) is False:
                    continue
                min_alt = _optional_float(props.get("min_alt_ft"))
                max_alt = _optional_float(props.get("max_alt_ft"))
                if min_alt is not None and max_alt is not None and min_alt > max_alt:
                    LOGGER.warning(
                        "Skipping geofence %s:%s with min_alt_ft > max_alt_ft",
                        layer_id,
                        index + 1,
                    )
                    continue
                code = str(props["code"]).strip() if props.get("code") else None
                target.append(
                    Geofence(
                        layer_id=layer_id,
                        name=str(props.get("name") or f"{layer_id}:{index + 1}"),
                        geometry=prep(geometry),
                        shape=geometry,
                        min_alt_ft=min_alt,
                        max_alt_ft=max_alt,
                        min_alt_exclusive=_optional_bool(
                            props.get("min_alt_exclusive"), False
                        ),
                        code=code,
                        priority=_optional_int(props.get("control_priority"), 20 if code else 0),
                    )
                )
            except (TypeError, ValueError) as exc:
                LOGGER.warning("Skipping geofence properties %s:%s: %s", layer_id, index + 1, exc)

    @staticmethod
    def _kml_to_geojson(path: Path) -> dict[str, Any]:
        root = ElementTree.parse(path).getroot()
        features: list[dict[str, Any]] = []
        for index, placemark in enumerate(root.findall(".//k:Placemark", KML_NS)):
            name_element = placemark.find("k:name", KML_NS)
            name = name_element.text if name_element is not None else f"Feature {index + 1}"
            geometry = _kml_geometry(placemark)
            if geometry:
                features.append(
                    {
                        "type": "Feature",
                        "properties": {"name": name},
                        "geometry": geometry,
                    }
                )
        return {"type": "FeatureCollection", "name": path.stem, "features": features}


def _keep_layer(
    previous: LayerCatalog,
    layer_id: str,
    layers: dict[str, dict[str, Any]],
    files: dict[str, Path],
    vectors: dict[str, dict[str, Any]],
    geofences: list[Geofence],
    fingerprints: dict[str, tuple[int, int]],
) -> None:
    layers[layer_id] = previous.layers[layer_id]
    if layer_id in previous.files:
        files[layer_id] = previous.files[layer_id]
    if layer_id in previous.vectors:
        vectors[layer_id] = previous.vectors[layer_id]
    if layer_id in previous.fingerprints:
        fingerprints[layer_id] = previous.fingerprints[layer_id]
    geofences.extend(item for item in previous.geofences if item.layer_id == layer_id)


def _public_gis_error(item: dict[str, Any] | object) -> dict[str, Any]:
    error = dict(item)
    error["reason"] = str(error.get("reason") or error.get("error") or "")
    if "kept_previous" in error:
        error["kept_previous"] = error["kept_previous"] in {True, "true", "1"}
    return error


def _geofence_tree(geofences: tuple[Geofence, ...]) -> STRtree | None:
    if not geofences:
        return None
    return STRtree([item.shape for item in geofences])


def _sqlite_open(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.execute("PRAGMA query_only = 1")
    return connection


def _mbtiles_metadata(path: Path) -> dict[str, str]:
    with closing(_sqlite_open(path)) as connection:
        names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        if "metadata" not in names or "tiles" not in names:
            raise ValueError("MBTiles must contain metadata and tiles tables")
        return dict(connection.execute("SELECT name, value FROM metadata").fetchall())


def _file_fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _normalize_feature_collection(data: object, path: Path) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise ValueError(f"{path}: expected a GeoJSON FeatureCollection")
    features = data.get("features")
    if not isinstance(features, list):
        raise ValueError(f"{path}: FeatureCollection.features must be an array")
    if len(features) > MAX_FEATURES:
        raise ValueError(f"{path}: too many features ({len(features)} > {MAX_FEATURES})")
    cleaned: list[dict[str, Any]] = []
    positions = 0
    for index, feature in enumerate(features):
        item = _normalize_feature(feature, path, index)
        if item is None:
            continue
        positions += _count_positions(item["geometry"].get("coordinates"))
        if positions > MAX_POSITIONS:
            raise ValueError(f"{path}: too many coordinates (>{MAX_POSITIONS})")
        cleaned.append(item)
    result = dict(data)
    result["features"] = cleaned
    return result


def _normalize_feature(feature: object, path: Path, index: int) -> dict[str, Any] | None:
    if not isinstance(feature, dict) or feature.get("type") != "Feature":
        LOGGER.warning("%s: skipping non-Feature at %s", path, index)
        return None
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict) or not geometry.get("type"):
        return None
    try:
        _assert_finite_coordinates(geometry.get("coordinates"))
    except ValueError as exc:
        LOGGER.warning("%s: skipping feature %s: %s", path, index, exc)
        return None
    properties = feature.get("properties")
    if properties is None:
        properties = {}
    if not isinstance(properties, dict):
        LOGGER.warning("%s: skipping feature %s with non-object properties", path, index)
        return None
    return {
        "type": "Feature",
        "properties": properties,
        "geometry": geometry,
    }


def _assert_finite_coordinates(value: object) -> None:
    if value is None:
        return
    if isinstance(value, (list, tuple)):
        if value and isinstance(value[0], (int, float)) and not isinstance(value[0], bool):
            if len(value) < 2:
                raise ValueError("coordinate pair required")
            lon = float(value[0])
            lat = float(value[1])
            if not (math.isfinite(lon) and math.isfinite(lat)):
                raise ValueError("non-finite coordinate")
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError("coordinate out of range")
            for extra in value[2:]:
                if isinstance(extra, bool) or not isinstance(extra, (int, float)):
                    raise ValueError("invalid coordinate structure")
                if not math.isfinite(float(extra)):
                    raise ValueError("non-finite coordinate")
            return
        for item in value:
            _assert_finite_coordinates(item)
        return
    if isinstance(value, (int, float)):
        if isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError("non-finite coordinate")
        return
    raise ValueError("invalid coordinate structure")


def _count_positions(value: object) -> int:
    if not isinstance(value, (list, tuple)) or not value:
        return 0
    if isinstance(value[0], (int, float)):
        return 1
    return sum(_count_positions(item) for item in value)


def _kml_geometry(placemark: Any) -> dict[str, Any] | None:
    point = placemark.find("k:Point/k:coordinates", KML_NS)
    if point is not None and point.text:
        coords = _kml_coordinates(point.text)
        if coords:
            return {"type": "Point", "coordinates": coords[0]}
    line = placemark.find("k:LineString/k:coordinates", KML_NS)
    if line is not None and line.text:
        coords = _kml_coordinates(line.text)
        if coords:
            return {"type": "LineString", "coordinates": coords}
    polygon = placemark.find("k:Polygon", KML_NS)
    if polygon is not None:
        return _kml_polygon(polygon)
    return None


def _kml_polygon(polygon: Any) -> dict[str, Any] | None:
    outer = polygon.find("k:outerBoundaryIs/k:LinearRing/k:coordinates", KML_NS)
    if outer is None or not outer.text:
        return None
    rings = [_close_ring(_kml_coordinates(outer.text))]
    if len(rings[0]) < 4:
        return None
    for inner in polygon.findall("k:innerBoundaryIs/k:LinearRing/k:coordinates", KML_NS):
        if inner.text:
            hole = _close_ring(_kml_coordinates(inner.text))
            if len(hole) >= 4:
                rings.append(hole)
    return {"type": "Polygon", "coordinates": rings}


def _kml_coordinates(text: str) -> list[list[float]]:
    points: list[list[float]] = []
    for part in text.split():
        pieces = part.split(",")
        if len(pieces) < 2:
            continue
        lon = float(pieces[0])
        lat = float(pieces[1])
        if not (math.isfinite(lon) and math.isfinite(lat)):
            raise ValueError("non-finite KML coordinate")
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("KML coordinate out of range")
        points.append([lon, lat])
    return points


def _close_ring(coords: list[list[float]]) -> list[list[float]]:
    if len(coords) >= 3 and coords[0] != coords[-1]:
        return [*coords, coords[0]]
    return coords


def _optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _optional_int(value: object, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(float(str(value)))
    except (TypeError, ValueError):
        return default
    return number


def _optional_bool(value: object, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean {value!r}")
