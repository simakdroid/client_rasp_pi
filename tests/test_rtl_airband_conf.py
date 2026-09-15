import json
from pathlib import Path

import pytest

from app.config import RadioChannel
from app.rtl_airband_conf import (
    load_channels_json,
    parse_env_file,
    render_conf,
    tuner_center_mhz,
)

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


def test_render_uses_user_frequencies(tmp_path) -> None:
    text = render_conf(
        channels_json=json.dumps(
            [
                {"id": "tower", "name": "Вышка", "frequency_mhz": 118.1},
                {"id": "ground", "name": "Земля", "frequency_mhz": 118.5},
            ]
        ),
        vhf_serial="0118",
        icecast_host="127.0.0.1",
        icecast_port="8000",
        icecast_user="source",
        icecast_password="s3cret",
    )
    assert 'serial = "0118";' in text
    assert "freq = 118.100;" in text
    assert "freq = 118.500;" in text
    assert 'mountpoint = "vhf-118100.mp3";' in text
    assert 'password = "s3cret";' in text
    assert "centerfreq = 118.300;" in text


def test_wide_span_is_rejected() -> None:
    with pytest.raises(ValueError, match="does not fit"):
        tuner_center_mhz([118.1, 125.8])


def test_optional_stream_url_defaults() -> None:
    channel = RadioChannel(id="tower", name="Вышка", frequency_mhz=121.7)
    assert channel.mountpoint == "vhf-121700.mp3"
    assert channel.stream_url == "http://127.0.0.1:8000/vhf-121700.mp3"


def test_example_backend_env_renders_default_catalog() -> None:
    env = ROOT / "deploy" / "env" / "backend.env.example"
    text = render_conf(
        channels_json=load_channels_json(backend_env=env),
        icecast_password="x",
    )
    catalog = json.loads((ROOT / "deploy" / "radio-channels.json").read_text(encoding="utf-8"))
    for item in catalog:
        freq = f"{float(item['frequency_mhz']):.3f}"
        assert f"freq = {freq};" in text
        assert f'mountpoint = "{item["mountpoint"]}";' in text
