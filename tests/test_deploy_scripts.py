import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "deploy" / "scripts" / "rtl-device-mode.sh"


pytestmark = pytest.mark.skipif(os.name == "nt", reason="requires a POSIX shell")


def test_single_blank_serial_uses_index_zero(tmp_path) -> None:
    _add_rtl(tmp_path, "1-1", serial=None)

    assert _run_selector(tmp_path, "count") == "1"
    assert _run_selector(tmp_path, "adsb-device") == "0"


def test_two_receivers_use_preferred_adsb_serial(tmp_path) -> None:
    _add_rtl(tmp_path, "1-1", serial="1090")
    _add_rtl(tmp_path, "1-2", serial="0118")

    assert _run_selector(tmp_path, "count") == "2"
    assert _run_selector(tmp_path, "adsb-device") == "1090"


def _add_rtl(root: Path, name: str, serial: str | None) -> None:
    device = root / name
    device.mkdir()
    (device / "idVendor").write_text("0bda\n", encoding="ascii")
    (device / "idProduct").write_text("2838\n", encoding="ascii")
    if serial is not None:
        (device / "serial").write_text(f"{serial}\n", encoding="ascii")


def _run_selector(sysfs: Path, command: str) -> str:
    environment = {
        **os.environ,
        "RTL_SYSFS_ROOT": str(sysfs),
        "ADSB_PREFERRED_SERIAL": "1090",
    }
    result = subprocess.run(
        ["/bin/sh", str(SCRIPT), command],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.strip()
