import asyncio

import pytest

from app.config import RadioChannel
from app.radio import (
    IcecastStreamError,
    RadioMonitor,
    _parse_prometheus_stats,
    open_icecast_audio,
    rewrite_loopback_stream_url,
)


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


@pytest.mark.asyncio
async def test_radio_hidden_when_vhf_serial_is_duplicated(tmp_path) -> None:
    channel = RadioChannel(
        id="tower",
        name="Tower",
        frequency_mhz=118.1,
        stream_url="http://127.0.0.1:8000/tower",
    )
    monitor = RadioMonitor([channel], auto_detect=True, sysfs_path=tmp_path)
    _add_rtl_device(tmp_path, "usb1", "1090")
    _add_rtl_device(tmp_path, "usb2", "0118")
    _add_rtl_device(tmp_path, "usb3", "0118")

    assert await monitor.status() == []


def test_rewrite_loopback_stream_url_keeps_icecast_port() -> None:
    url = "http://127.0.0.1:8000/vhf-118100.mp3"
    assert rewrite_loopback_stream_url(url, "192.168.1.10") == (
        "http://192.168.1.10:8000/vhf-118100.mp3"
    )
    assert rewrite_loopback_stream_url(url, "localhost") == url
    assert rewrite_loopback_stream_url(url, "127.0.0.1") == url
    assert rewrite_loopback_stream_url("http://icecast.lan:8000/vhf.mp3", "pi") == (
        "http://icecast.lan:8000/vhf.mp3"
    )


def test_sdr_roles_and_quality_omit_stream_urls(tmp_path) -> None:
    _add_rtl_device(tmp_path, "usb1", "1090")
    monitor = RadioMonitor(
        [
            RadioChannel(
                id="tower",
                name="Tower",
                frequency_mhz=118.1,
                stream_url="http://user:pass@127.0.0.1:8000/tower",
            )
        ],
        auto_detect=False,
        sysfs_path=tmp_path,
    )
    roles = monitor.role_snapshot("1090")
    assert roles["adsb"] == "0"
    assert roles["vhf"] is None
    _add_rtl_device(tmp_path, "usb2", "0118")
    roles = monitor.role_snapshot("1090")
    assert roles["adsb"] == "1090"
    assert roles["vhf"] == "0118"


@pytest.mark.asyncio
async def test_radio_quality_snapshot_omits_stream_url() -> None:
    channel = RadioChannel(
        id="tower",
        name="Tower",
        frequency_mhz=118.1,
        stream_url="http://user:pass@127.0.0.1:8000/tower",
    )
    monitor = RadioMonitor([channel], auto_detect=False)
    quality = await monitor.quality_snapshot()
    assert quality[0]["id"] == "tower"
    assert "stream_url" not in quality[0]


@pytest.mark.asyncio
async def test_open_icecast_audio_streams_mp3_body() -> None:
    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.0 200 OK\r\nContent-Type: audio/mpeg\r\n\r\nID3fake")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer, leftover, content_type = await open_icecast_audio(
            host="127.0.0.1",
            port=port,
        )
        try:
            body = leftover + await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
        assert content_type == "audio/mpeg"
        assert body.startswith(b"ID3")
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_open_icecast_audio_maps_missing_mount() -> None:
    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.0 404 File Not Found\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(IcecastStreamError) as caught:
            await open_icecast_audio(host="127.0.0.1", port=port)
        assert caught.value.status_code == 502
    finally:
        server.close()
        await server.wait_closed()


def _add_rtl_device(root, name: str, serial: str) -> None:
    device = root / name
    device.mkdir()
    (device / "idVendor").write_text("0bda\n", encoding="ascii")
    (device / "idProduct").write_text("2838\n", encoding="ascii")
    (device / "serial").write_text(f"{serial}\n", encoding="ascii")
