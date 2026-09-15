import json
from pathlib import Path

from app.config import SCAN_STREAM_URL, RadioChannel, Settings
from app.rtl_airband_conf import load_channels_json, parse_env_file, render_conf

ROOT = Path(__file__).resolve().parents[1]


def test_parse_quoted_backend_env(tmp_path) -> None:
    env = tmp_path / "backend.env"
    env.write_text(
        "AIRMON_ADMIN_TOKEN=secret\n"
        "AIRMON_RADIO_CHANNELS_JSON='[{\"id\":\"tower\",\"name\":\"Вышка\","
        "\"frequency_mhz\":121.7}]'\n",
        encoding="utf-8",
    )
    parsed = parse_env_file(env)
    assert parsed["AIRMON_ADMIN_TOKEN"] == "secret"
    channels = json.loads(parsed["AIRMON_RADIO_CHANNELS_JSON"])
    assert channels[0]["frequency_mhz"] == 121.7
    loaded = json.loads(load_channels_json(backend_env=env))
    assert loaded[0]["id"] == "tower"


def test_render_scan_allows_wide_span() -> None:
    text = render_conf(
        channels_json=json.dumps(
            [
                {"id": "tower", "name": "Вышка", "frequency_mhz": 118.1},
                {"id": "atis", "name": "ATIS", "frequency_mhz": 123.7},
            ]
        ),
        vhf_serial="0118",
        icecast_host="127.0.0.1",
        icecast_port="8000",
        icecast_user="source",
        icecast_password="s3cret",
    )
    assert 'mode = "scan";' in text
    assert "freqs = ( 118.100, 123.700 );" in text
    assert 'labels = ( "Вышка", "ATIS" );' in text
    assert 'mountpoint = "vhf-scan.mp3";' in text
    assert 'password = "s3cret";' in text
    assert "centerfreq" not in text
    assert "multichannel" not in text


def test_optional_stream_url_defaults() -> None:
    channel = RadioChannel(id="tower", name="Вышка", frequency_mhz=121.7)
    assert channel.mountpoint == "vhf-121700.mp3"
    assert channel.stream_url == "http://127.0.0.1:8000/vhf-121700.mp3"


def test_settings_share_scan_stream() -> None:
    settings = Settings(
        _env_file=None,
        radio_channels_json=(
            '[{"id":"tower","name":"Вышка","frequency_mhz":118.1},'
            '{"id":"atis","name":"ATIS","frequency_mhz":123.7}]'
        ),
    )
    channels = settings.radio_channels
    assert {item.stream_url for item in channels} == {SCAN_STREAM_URL}
    assert {item.mountpoint for item in channels} == {"vhf-scan.mp3"}


def test_example_backend_env_renders_scan_list() -> None:
    env = ROOT / "deploy" / "env" / "backend.env.example"
    text = render_conf(
        channels_json=load_channels_json(backend_env=env),
        icecast_password="x",
    )
    catalog = json.loads((ROOT / "deploy" / "radio-channels.json").read_text(encoding="utf-8"))
    assert 'mode = "scan";' in text
    assert 'mountpoint = "vhf-scan.mp3";' in text
    for item in catalog:
        assert f"{float(item['frequency_mhz']):.3f}" in text
