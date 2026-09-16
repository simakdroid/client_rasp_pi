"""Build GeoJSON layers for USSV / Yekaterinburg airspace from AIP coordinates."""

from __future__ import annotations

import argparse
import json
import math
import re
import struct
from pathlib import Path

from shapely.geometry import Point, Polygon, box
from shapely.ops import nearest_points, unary_union

ROOT = Path(__file__).resolve().parents[1]
LAYERS = ROOT / "data" / "layers"
DEFAULT_SUBJECTS_SHP = Path(r"c:\Users\dns\Downloads\ru-subjects-contour-shp\ru-subjects-contour.shp")
BORDER_SUBJECTS = {"RU-BA", "RU-CHE", "RU-KGN", "RU-ORE", "RU-TYU"}
BORDER_ROI = box(59.5, 51.5, 69.5, 55.8)

COORD_RE = re.compile(r"(\d{6})([NSns])\s+(\d{7})([EWewЕе])")
M_TO_FT = 1 / 0.3048

COLORS = {
    "U1": "#e57373",
    "U2": "#ffb74d",
    "U3": "#fff176",
    "U4": "#81c784",
    "U5": "#4db6ac",
    "U6": "#4fc3f7",
    "U7": "#7986cb",
    "U8": "#ba68c8",
    "U9": "#f06292",
    "U10": "#a1887f",
    "USSS_APP": "#ff9800",
    "USSV": "#90caf9",
}


def dms_to_deg(value: str, hemi: str) -> float:
    hemi = hemi.upper().replace("Е", "E")
    if len(value) == 6:
        degrees, minutes, seconds = int(value[:2]), int(value[2:4]), int(value[4:6])
    elif len(value) == 7:
        degrees, minutes, seconds = int(value[:3]), int(value[3:5]), int(value[5:7])
    else:
        raise ValueError(f"bad DMS {value}")
    decimal = degrees + minutes / 60 + seconds / 3600
    if hemi in {"S", "W"}:
        decimal = -decimal
    return round(decimal, 6)


def parse_points(text: str) -> list[list[float]]:
    matches = COORD_RE.findall(text.replace("Е", "E"))
    if not matches:
        raise ValueError(f"no coordinates in {text[:80]!r}")
    points = [
        [dms_to_deg(lon, lon_h), dms_to_deg(lat, lat_h)]
        for lat, lat_h, lon, lon_h in matches
    ]
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]
    return points


def point_key(point: list[float]) -> tuple[float, float]:
    return (round(point[0], 5), round(point[1], 5))


def parse_ring(text: str, border: SubjectBorder | None = None) -> list[list[float]]:
    points = parse_points(text)
    if border is not None:
        spliced = [points[0]]
        for start, end in zip(points, points[1:] + [points[0]]):
            path = border.between(start, end)
            if path:
                spliced.extend(path)
            spliced.append(end)
        points = spliced[:-1]
    ring = [list(point) for point in points]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    polygon = Polygon(ring)
    if not polygon.exterior.is_ccw:
        ring = list(reversed(ring))
        polygon = Polygon(ring)
    if not polygon.is_valid:
        raise ValueError(f"invalid polygon: {polygon.explain_validity()}")
    return [[round(x, 6), round(y, 6)] for x, y in ring]


class SubjectBorder:
    """Russia–Kazakhstan contour from a subjects shapefile, between AIP vertices."""

    def __init__(self, shp_path: Path) -> None:
        if not shp_path.is_file():
            raise FileNotFoundError(shp_path)
        union = unary_union(list(_read_border_subjects(shp_path)))
        if union.geom_type == "MultiPolygon":
            union = max(union.geoms, key=lambda geom: geom.area)
        self.union = union
        self.coords = list(union.exterior.coords)[:-1]
        self.spans = {
            frozenset((
                point_key(parse_points(start)[0]),
                point_key(parse_points(end)[0]),
            ))
            for start, end in (
                ("552400N 0690900E", "541200N 0631900E"),
                ("541200N 0631900E", "515900N 0600000E"),
                ("552400N 0690900E", "515900N 0600000E"),
            )
        }

    def between(self, start: list[float], end: list[float]) -> list[list[float]]:
        if frozenset((point_key(start), point_key(end))) not in self.spans:
            return []
        start_pt = Point(start)
        end_pt = Point(end)
        start_on = nearest_points(start_pt, self.union.boundary)[1]
        end_on = nearest_points(end_pt, self.union.boundary)[1]
        i0 = self._nearest_index(start_on.x, start_on.y)
        i1 = self._nearest_index(end_on.x, end_on.y)
        forward = self._walk(i0, i1, 1)
        reverse = self._walk(i0, i1, -1)
        chosen = forward if _path_length(forward) <= _path_length(reverse) else reverse
        path = [_round_point(start_on.x, start_on.y)]
        path.extend(_round_point(x, y) for x, y in chosen[1:-1])
        path.append(_round_point(end_on.x, end_on.y))
        cleaned: list[list[float]] = []
        for point in path:
            if not cleaned or point != cleaned[-1]:
                if point_key(point) in {point_key(start), point_key(end)}:
                    continue
                cleaned.append(point)
        print(
            f"border {start[0]:.5f},{start[1]:.5f} -> {end[0]:.5f},{end[1]:.5f}: "
            f"{len(cleaned)} vertices"
        )
        return cleaned

    def _nearest_index(self, x: float, y: float) -> int:
        best = 0
        best_dist = math.inf
        for index, (cx, cy) in enumerate(self.coords):
            dist = (cx - x) ** 2 + (cy - y) ** 2
            if dist < best_dist:
                best_dist = dist
                best = index
        return best

    def _walk(self, start: int, end: int, step: int) -> list[tuple[float, float]]:
        path: list[tuple[float, float]] = []
        index = start
        count = len(self.coords)
        while True:
            path.append(self.coords[index])
            if index == end:
                return path
            index = (index + step) % count


def _round_point(x: float, y: float) -> list[float]:
    return [round(x, 6), round(y, 6)]


def _path_length(path: list[tuple[float, float]]) -> float:
    return sum(
        math.hypot(b[0] - a[0], b[1] - a[1])
        for a, b in zip(path, path[1:])
    )


def _read_border_subjects(shp_path: Path):
    dbf_path = shp_path.with_suffix(".dbf")
    shp = shp_path.read_bytes()
    dbf = dbf_path.read_bytes()
    _ver, _yy, _mm, _dd, nrec, header, rec_len = struct.unpack("<BBBBLHH", dbf[:12])
    offset = 32
    fields: list[tuple[str, int]] = []
    while dbf[offset] != 0x0D:
        name = dbf[offset:offset + 11].split(b"\x00", 1)[0].decode("ascii")
        length = dbf[offset + 16]
        fields.append((name, length))
        offset += 32
    iso_index = next(i for i, (name, _length) in enumerate(fields) if name == "iso_3166_2")
    records = []
    pos = header
    for _ in range(nrec):
        raw = dbf[pos:pos + rec_len]
        pos += rec_len
        cursor = 1
        values = []
        for _name, length in fields:
            values.append(raw[cursor:cursor + length].decode("utf-8", "replace").strip())
            cursor += length
        records.append(values[iso_index])
    pos = 100
    for iso in records:
        _rec_num, rec_len_words = struct.unpack(">ii", shp[pos:pos + 8])
        pos += 8
        content = shp[pos:pos + rec_len_words * 2]
        pos += rec_len_words * 2
        if iso not in BORDER_SUBJECTS:
            continue
        _xmin, _ymin, _xmax, _ymax, nparts, npoints = struct.unpack("<4dii", content[4:44])
        parts = struct.unpack(f"<{nparts}i", content[44:44 + 4 * nparts])
        pts_off = 44 + 4 * nparts
        pts = list(struct.iter_unpack("<2d", content[pts_off:pts_off + 16 * npoints]))
        rings = []
        for part in range(nparts):
            start = parts[part]
            stop = parts[part + 1] if part + 1 < nparts else npoints
            ring = list(pts[start:stop])
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            rings.append(ring)
        polygon = Polygon(rings[0], rings[1:] if len(rings) > 1 else None)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.intersects(BORDER_ROI):
            yield polygon


def meters_amsl(min_m: int) -> dict:
    return {
        "min_alt_ft": round(min_m * M_TO_FT),
        "min_alt_exclusive": True,
    }


def flight_level(min_fl: int | None = None, max_fl: int | None = None) -> dict:
    props: dict = {}
    if min_fl is not None:
        props["min_alt_ft"] = min_fl * 100
        props["min_alt_exclusive"] = True
    if max_fl is not None:
        props["max_alt_ft"] = max_fl * 100
    return props


def feature(name: str, code: str | None, color: str, ring: list[list[float]], extra: dict, priority: int) -> dict:
    props: dict = {"name": name}
    if code:
        props["code"] = code
        props["control_priority"] = priority
    props.update({
        "geofence": True,
        "color": color,
        **extra,
    })
    return {
        "type": "Feature",
        "properties": props,
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def collection(name: str, features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "name": name, "features": features}


def dump(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False, indent=2)
    compact = re.sub(
        r"\[\s+(-?\d+\.?\d*),\s+(-?\d+\.?\d*)\s+\]",
        r"[\1, \2]",
        raw,
    )
    path.write_text(compact + "\n", encoding="utf-8")
    print(f"{path.name}: {len(data['features'])} features")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subjects-shp", type=Path, default=DEFAULT_SUBJECTS_SHP)
    args = parser.parse_args(argv)
    border = SubjectBorder(args.subjects_shp)
    sectors = [
        feature(
            "Сектор Урал 1",
            "U1",
            COLORS["U1"],
            parse_ring(
                "585211N 0605743E 584434N 0612821E 575919N 0601412E "
                "573119N 0603624E 564436N 0604749E 562246N 0610116E "
                "555436N 0605900E 555500N 0595900E 560500N 0592900E "
                "554200N 0575300E 563000N 0560500E 572205N 0565639E "
                "580018N 0583330E 583844N 0581822E 585211N 0605743E"
            ),
            meters_amsl(1500),
            20,
        ),
        feature(
            "Сектор Урал 2",
            "U2",
            COLORS["U2"],
            parse_ring(
                "620500N 0631200E 611700N 0634000E 604500N 0634200E "
                "593000N 0640800E 590200N 0634400E 584248N 0635036E "
                "582934N 0622645E 585211N 0605743E 620000N 0593000E "
                "620500N 0631200E"
            ),
            meters_amsl(1500),
            20,
        ),
        feature(
            "Сектор Урал 2",
            "U2",
            COLORS["U2"],
            parse_ring(
                "620000N 0593000E 585211N 0605743E 583844N 0581822E "
                "611000N 0571300E 620000N 0593000E"
            ),
            meters_amsl(2700),
            20,
        ),
        feature(
            "Сектор Урал 3",
            "U3",
            COLORS["U3"],
            parse_ring(
                "584434N 0612821E 582934N 0622645E 584248N 0635036E "
                "574900N 0640800E 572300N 0634500E 570000N 0634500E "
                "561626N 0633115E 553959N 0625039E 554341N 0622215E "
                "555400N 0614400E 555436N 0605900E 562246N 0610116E "
                "564436N 0604749E 573119N 0603624E 575919N 0601412E "
                "584434N 0612821E"
            ),
            meters_amsl(1500),
            20,
        ),
        feature(
            "Сектор Урал 4",
            "U4",
            COLORS["U4"],
            parse_ring(
                "570000N 0634500E 565200N 0650200E 554900N 0682000E "
                "554000N 0691300E 552400N 0690900E 541200N 0631900E "
                "552321N 0630453E 553959N 0625039E 561626N 0633115E "
                "570000N 0634500E",
                border,
            ),
            meters_amsl(1500),
            20,
        ),
        feature(
            "Сектор Урал 5",
            "U5",
            COLORS["U5"],
            parse_ring(
                "544349N 0595456E 540105N 0591823E 532415N 0584527E "
                "523500N 0581900E 531600N 0573400E 541200N 0583900E "
                "544219N 0582354E 544349N 0595456E"
            ),
            meters_amsl(2700),
            20,
        ),
        feature(
            "Сектор Урал 5",
            "U5",
            COLORS["U5"],
            parse_ring(
                "550918N 0630743E 541200N 0631900E 515900N 0600000E "
                "523500N 0581900E 532415N 0584527E 540105N 0591823E "
                "544349N 0595456E 544407N 0603425E 544730N 0610912E "
                "550040N 0614751E 550918N 0630743E",
                border,
            ),
            meters_amsl(1500),
            20,
        ),
        feature(
            "Сектор Урал 6",
            "U6",
            COLORS["U6"],
            parse_ring(
                "560500N 0592900E 555500N 0595900E 552933N 0601121E "
                "545111N 0600123E 544349N 0595456E 544219N 0582354E "
                "554200N 0575300E 560500N 0592900E"
            ),
            meters_amsl(2700),
            20,
        ),
        feature(
            "Сектор Урал 6",
            "U6",
            COLORS["U6"],
            parse_ring(
                "555500N 0595900E 555400N 0614400E 554341N 0622215E "
                "553959N 0625039E 552321N 0630453E 550918N 0630743E "
                "550040N 0614751E 544730N 0610912E 544407N 0603425E "
                "544349N 0595456E 545111N 0600123E 552933N 0601121E "
                "555500N 0595900E"
            ),
            meters_amsl(1500),
            20,
        ),
        feature(
            "Сектор Урал 7",
            "U7",
            COLORS["U7"],
            parse_ring(
                "611000N 0571300E 583844N 0581822E 581534N 0555548E "
                "582645N 0520646E 600000N 0520000E 604300N 0554200E "
                "611000N 0571300E"
            ),
            meters_amsl(1500),
            20,
        ),
        feature(
            "Сектор Урал 8",
            "U8",
            COLORS["U8"],
            parse_ring(
                "583844N 0581822E 580018N 0583330E 572205N 0565639E "
                "563000N 0560500E 563000N 0534000E 562500N 0521200E "
                "572425N 0521059E 582645N 0520646E 581534N 0555548E "
                "583844N 0581822E"
            ),
            meters_amsl(1500),
            20,
        ),
        feature(
            "Сектор Урал 9",
            "U9",
            COLORS["U9"],
            parse_ring(
                "602000N 0484000E 600000N 0500000E 600000N 0520000E "
                "592028N 0520257E 591126N 0495335E 584636N 0485004E "
                "582525N 0483015E 581000N 0465600E 584600N 0450000E "
                "593100N 0453000E 600400N 0462000E 602000N 0484000E"
            ),
            meters_amsl(1500),
            20,
        ),
        feature(
            "Сектор Урал 10",
            "U10",
            COLORS["U10"],
            parse_ring(
                "592028N 0520257E 572425N 0521059E 562500N 0521200E "
                "562200N 0515300E 562800N 0504800E 571800N 0475500E "
                "571400N 0464500E 581000N 0465600E 582525N 0483015E "
                "584636N 0485004E 591126N 0495335E 592028N 0520257E"
            ),
            meters_amsl(1500),
            20,
        ),
    ]
    app = [
        feature(
            "Екатеринбург/Кольцово УДР",
            "USSS_APP",
            COLORS["USSS_APP"],
            parse_ring(
                "572012N 0615547E 565741N 0620349E 564406N 0614912E "
                "564700N 0614300E 564400N 0612230E 564221N 0611813E "
                "564039N 0611613E 563806N 0611530E 563400N 0611500E "
                "562700N 0613100E 562000N 0614000E 561042N 0621242E "
                "555400N 0614400E 555500N 0595900E 560500N 0592900E "
                "562549N 0590659E 565101N 0590354E 570419N 0591036E "
                "572012N 0615547E"
            ),
            {**meters_amsl(1500), **flight_level(max_fl=190)},
            30,
        ),
        feature(
            "Екатеринбург/Кольцово УДР",
            "USSS_APP",
            COLORS["USSS_APP"],
            parse_ring(
                "564700N 0614300E 564406N 0614912E 562700N 0613100E "
                "563400N 0611500E 563806N 0611530E 564039N 0611613E "
                "564221N 0611813E 564400N 0612230E 564700N 0614300E"
            ),
            flight_level(min_fl=70, max_fl=190),
            30,
        ),
    ]
    fir = [
        feature(
            "РПИ Екатеринбург",
            None,
            COLORS["USSV"],
            parse_ring(
                "620500N 0631200E 611700N 0634000E 604500N 0634200E "
                "593000N 0640800E 590200N 0634400E 574900N 0640800E "
                "572300N 0634500E 570000N 0634500E 565200N 0650200E "
                "554900N 0682000E 554000N 0691300E 552400N 0690900E "
                "515900N 0600000E 523500N 0581900E 532415N 0584527E "
                "540105N 0591823E 545111N 0600123E 552933N 0601121E "
                "555500N 0595900E 560500N 0592900E 554200N 0575300E "
                "563000N 0560500E 563000N 0534000E 562500N 0521200E "
                "562200N 0515300E 562800N 0504800E 571800N 0475500E "
                "571400N 0464500E 581000N 0465600E 584600N 0450000E "
                "593100N 0453000E 600400N 0462000E 602000N 0484000E "
                "600000N 0500000E 600000N 0520000E 604300N 0554200E "
                "611000N 0571300E 583844N 0581822E 585211N 0605743E "
                "620000N 0593000E 620500N 0631200E",
                border,
            ),
            {},
            0,
        ),
    ]
    dump(LAYERS / "acc-ural.geojson", collection("Секторы РПИ Екатеринбург", sectors))
    dump(LAYERS / "udr-yekaterinburg-koltsovo.geojson", collection("УДР Екатеринбург/Кольцово", app))
    dump(LAYERS / "rpi-yekaterinburg.geojson", collection("РПИ Екатеринбург", fir))


if __name__ == "__main__":
    main()
