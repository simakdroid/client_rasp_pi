import pytest

from app.estimate import coast_position, radio_horizon_km


def test_radio_horizon_uses_altitude_and_caps() -> None:
    ground = radio_horizon_km(0, 30, 450)
    cruise = radio_horizon_km(35000, 30, 450)
    unknown = radio_horizon_km(None, 30, 450)
    on_ground = radio_horizon_km(12000, 30, 450, on_ground=True)
    capped = radio_horizon_km(45000, 30, 200)

    assert ground == 22.6
    assert cruise == 448.1
    assert unknown is None
    assert on_ground == ground
    assert capped == 200.0


def test_coast_position_moves_along_ground_track() -> None:
    # 360 kt for 10 s is 1 NM ≈ 1852 m north.
    result = coast_position(55.0, 37.0, 0.0, 360.0, 10.0)
    assert result is not None
    lat, lon = result
    assert lat == pytest.approx(55.01664, abs=0.0002)
    assert lon == pytest.approx(37.0, abs=0.0002)
    assert coast_position(55.0, 37.0, 0.0, 360.0, 0) is None
    assert coast_position(55.0, 37.0, 90.0, 0.0, 10) is None
