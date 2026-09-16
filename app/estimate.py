from __future__ import annotations

from pyproj import Geod

GEOD = Geod(ellps="WGS84")
KNOTS_TO_MS = 1852.0 / 3600.0
FT_TO_M = 0.3048
# Optical/radio horizon with 4/3 Earth refraction, km per sqrt(metres).
HORIZON_KM_PER_SQRT_M = 4.12


def radio_horizon_km(
    altitude_ft: int | None,
    station_alt_m: float,
    max_km: float,
    on_ground: bool | None = None,
) -> float | None:
    """Maximum line-of-sight range from the station, or None if altitude is unknown."""
    if on_ground is True:
        altitude_ft = 0
    if altitude_ft is None:
        return None
    aircraft_m = max(0.0, float(altitude_ft) * FT_TO_M)
    station_m = max(0.0, float(station_alt_m))
    range_km = HORIZON_KM_PER_SQRT_M * (station_m**0.5 + aircraft_m**0.5)
    return round(min(max(range_km, 0.0), float(max_km)), 1)


def coast_position(
    lat: float,
    lon: float,
    heading_deg: float,
    speed_kt: float,
    age_s: float,
) -> tuple[float, float] | None:
    """Extrapolate a measured position along ground track. Not a Mode-S fix."""
    if age_s <= 0 or speed_kt <= 0:
        return None
    distance_m = float(speed_kt) * KNOTS_TO_MS * float(age_s)
    new_lon, new_lat, _ = GEOD.fwd(lon, lat, heading_deg, distance_m)
    return round(new_lat, 6), round(new_lon, 6)
