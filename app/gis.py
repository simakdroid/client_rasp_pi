from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from defusedxml import ElementTree
from shapely.geometry import Point, shape
from shapely.prepared import PreparedGeometry, prep

SUPPORTED_VECTOR_SUFFIXES = {".geojson", ".json", ".kml"}
LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Geofence:
    layer_id: str
    name: str
    geometry: PreparedGeometry
    min_alt_ft: float | None = None
    max_alt_ft: float | None = None
    min_alt_exclusive: bool = False
    code: str | None = None
    priority: int = 0

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


class LayerManager:
    """Loads trusted, administrator-provided GIS files from one directory."""

    def __init__(self, layers_dir: Path) -> None:
        self.layers_dir = layers_dir
        self._layers: dict[str, dict[str, Any]] = {}
        self._files: dict[str, Path] = {}
        self._geofences: list[Geofence] = []

    def refresh(self) -> None:
        self.layers_dir.mkdir(parents=True, exist_ok=True)
        layers: dict[str, dict[str, Any]] = {}
        files: dict[str, Path] = {}
        geofences: list[Geofence] = []

        for path in sorted(self.layers_dir.iterdir()):
            suffix = path.suffix.lower()
            if not path.is_file() or suffix not in SUPPORTED_VECTOR_SUFFIXES | {".mbtiles"}:
                continue
            layer_id = path.stem
            if suffix == ".mbtiles":
                metadata = self._mbtiles_metadata(path)
                tile_format = metadata.get("format", "pbf").lower()
                vector_layers: list[str] = []
                if metadata.get("json"):
                    try:
                        vector_layers = [
                            str(item["id"])
                            for item in json.loads(metadata["json"]).get("vector_layers", [])
                            if item.get("id")
                        ]
                    except (json.JSONDecodeError, TypeError):
                        LOGGER.warning("Invalid vector layer metadata in %s", path)
                layers[layer_id] = {
                    "id": layer_id,
                    "name": metadata.get("name", layer_id),
                    "kind": "mbtiles",
                    "format": tile_format,
                    "tile_url": f"/api/tiles/{layer_id}/{{z}}/{{x}}/{{y}}",
                    "minzoom": _optional_float(metadata.get("minzoom")),
                    "maxzoom": _optional_float(metadata.get("maxzoom")),
                    "vector_layers": vector_layers,
                }
                files[layer_id] = path
                continue

            try:
                data = self._load_vector(path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                LOGGER.warning("Skipping invalid GIS layer %s: %s", path, exc)
                continue
            title = str(data.get("name") or layer_id)
            layers[layer_id] = {
                "id": layer_id,
                "name": title,
                "kind": "geojson",
                "feature_count": len(data.get("features", [])),
            }
            files[layer_id] = path
            self._append_geofences(layer_id, data, geofences)

        self._layers, self._files, self._geofences = layers, files, geofences

    def list_layers(self) -> list[dict[str, Any]]:
        return list(self._layers.values())

    def get_vector(self, layer_id: str) -> dict[str, Any]:
        path = self._files.get(layer_id)
        layer = self._layers.get(layer_id)
        if path is None or layer is None or layer["kind"] != "geojson":
            raise KeyError(layer_id)
        return self._load_vector(path)

    def matching_geofences(
        self, lon: float, lat: float, altitude_ft: int | None
    ) -> set[str]:
        return {
            geofence.name
            for geofence in self._geofences
            if geofence.contains(lon, lat, altitude_ft)
        }

    def matching_control_code(
        self, lon: float, lat: float, altitude_ft: int | None
    ) -> str | None:
        best: Geofence | None = None
        for geofence in self._geofences:
            if not geofence.code or not geofence.contains(lon, lat, altitude_ft):
                continue
            if best is None or geofence.priority > best.priority:
                best = geofence
        return None if best is None else best.code

    def get_tile(self, layer_id: str, z: int, x: int, y_xyz: int) -> bytes | None:
        path = self._files.get(layer_id)
        if path is None or self._layers[layer_id]["kind"] != "mbtiles":
            raise KeyError(layer_id)
        y_tms = (1 << z) - 1 - y_xyz
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            row = connection.execute(
                "SELECT tile_data FROM tiles "
                "WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
                (z, x, y_tms),
            ).fetchone()
        return bytes(row[0]) if row else None

    def tile_format(self, layer_id: str) -> str:
        layer = self._layers.get(layer_id)
        if layer is None or layer["kind"] != "mbtiles":
            raise KeyError(layer_id)
        return str(layer["format"])

    @staticmethod
    def _mbtiles_metadata(path: Path) -> dict[str, str]:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            return dict(connection.execute("SELECT name, value FROM metadata").fetchall())

    @staticmethod
    def _load_vector(path: Path) -> dict[str, Any]:
        if path.suffix.lower() == ".kml":
            return LayerManager._kml_to_geojson(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("type") != "FeatureCollection":
            raise ValueError(f"{path}: expected a GeoJSON FeatureCollection")
        return data

    @staticmethod
    def _append_geofences(
        layer_id: str, data: dict[str, Any], target: list[Geofence]
    ) -> None:
        for index, feature in enumerate(data.get("features", [])):
            geometry_data = feature.get("geometry")
            if not geometry_data:
                continue
            geometry = shape(geometry_data)
            if geometry.geom_type not in {"Polygon", "MultiPolygon"} or geometry.is_empty:
                continue
            props = feature.get("properties") or {}
            if props.get("geofence", True) is False:
                continue
            code = str(props["code"]).strip() if props.get("code") else None
            target.append(
                Geofence(
                    layer_id=layer_id,
                    name=str(props.get("name") or f"{layer_id}:{index + 1}"),
                    geometry=prep(geometry),
                    min_alt_ft=_optional_float(props.get("min_alt_ft")),
                    max_alt_ft=_optional_float(props.get("max_alt_ft")),
                    min_alt_exclusive=bool(props.get("min_alt_exclusive", False)),
                    code=code,
                    priority=_optional_int(props.get("control_priority"), 20 if code else 0),
                )
            )

    @staticmethod
    def _kml_to_geojson(path: Path) -> dict[str, Any]:
        root = ElementTree.parse(path).getroot()
        namespace = {"k": "http://www.opengis.net/kml/2.2"}
        features: list[dict[str, Any]] = []
        for index, placemark in enumerate(root.findall(".//k:Placemark", namespace)):
            name_element = placemark.find("k:name", namespace)
            name = name_element.text if name_element is not None else f"Feature {index + 1}"
            geometry: dict[str, Any] | None = None
            for kml_path, geojson_type in (
                ("k:Point/k:coordinates", "Point"),
                ("k:LineString/k:coordinates", "LineString"),
                (
                    "k:Polygon/k:outerBoundaryIs/k:LinearRing/k:coordinates",
                    "Polygon",
                ),
            ):
                coords_element = placemark.find(kml_path, namespace)
                if coords_element is None or not coords_element.text:
                    continue
                coordinates = [
                    [float(part.split(",")[0]), float(part.split(",")[1])]
                    for part in coords_element.text.split()
                ]
                if geojson_type == "Point":
                    coordinates = coordinates[0]
                elif geojson_type == "Polygon":
                    coordinates = [coordinates]
                geometry = {"type": geojson_type, "coordinates": coordinates}
                break
            if geometry:
                features.append(
                    {
                        "type": "Feature",
                        "properties": {"name": name},
                        "geometry": geometry,
                    }
                )
        return {"type": "FeatureCollection", "name": path.stem, "features": features}


def _optional_float(value: object) -> float | None:
    return None if value in (None, "") else float(value)


def _optional_int(value: object, default: int = 0) -> int:
    return default if value in (None, "") else int(float(str(value)))
