import pytest

from app.config import RadioChannel
from app.radio import RadioMonitor, _parse_prometheus_stats


def test_parse_rtl_airband_prometheus_stats() -> None:
    stats = _parse_prometheus_stats(
        'channel_activity_counter{freq="118.100",label="Tower"}\t42\n'
        'channel_dbfs_signal_level{freq="118.100",label="Tower"}\t-31.250\n'
    )
    assert stats["channel_activity_counter"]["118.100"] == 42
    assert stats["channel_dbfs_signal_level"]["118.100"] == -31.25


@pytest.mark.asyncio
async def test_radio_hidden_until_second_receiver_is_connected(tmp_path) -> None:
    channel = RadioChannel(
        id="tower",
        name="Tower",
        frequency_mhz=118.1,
        stream_url="http://127.0.0.1:8000/tower",
    )
    monitor = RadioMonitor([channel], auto_detect=True, sysfs_path=tmp_path)
    _add_rtl_device(tmp_path, "usb1", "1090")

    assert await monitor.status() == []

    _add_rtl_device(tmp_path, "usb2", "0118")
    channels = await monitor.status()
    assert len(channels) == 1
    assert channels[0]["id"] == "tower"


def _add_rtl_device(root, name: str, serial: str) -> None:
    device = root / name
    device.mkdir()
    (device / "idVendor").write_text("0bda\n", encoding="ascii")
    (device / "idProduct").write_text("2838\n", encoding="ascii")
    (device / "serial").write_text(f"{serial}\n", encoding="ascii")
