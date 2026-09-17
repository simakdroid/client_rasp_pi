from __future__ import annotations

from pathlib import Path
from typing import Any


class RadioMonitor:
    """Reports RTL-SDR roles without granting the web process systemd privileges."""

    def __init__(
        self,
        *,
        auto_detect: bool = True,
        min_receivers: int = 2,
        receiver_serial: str = "0118",
        sysfs_path: Path = Path("/sys/bus/usb/devices"),
    ) -> None:
        self.auto_detect = auto_detect
        self.min_receivers = min_receivers
        self.receiver_serial = receiver_serial
        self.sysfs_path = sysfs_path

    def hardware_available(self) -> bool:
        serials = _rtl_serials(self.sysfs_path)
        return (
            len(serials) >= self.min_receivers
            and serials.count(self.receiver_serial) == 1
        )

    def connected_serials(self) -> list[str]:
        return _rtl_serials(self.sysfs_path)

    def role_snapshot(self, preferred_adsb: str = "1090") -> dict[str, Any]:
        serials = self.connected_serials()
        count = len(serials)
        vhf = self.receiver_serial
        duplicate = serials.count(vhf) > 1 or (
            bool(preferred_adsb) and serials.count(preferred_adsb) > 1
        )
        adsb_role: str | None = None
        vhf_role: str | None = None
        if count == 1:
            adsb_role = "0"
        elif (
            count >= 2
            and preferred_adsb
            and preferred_adsb != vhf
            and serials.count(preferred_adsb) == 1
            and serials.count(vhf) == 1
        ):
            adsb_role = preferred_adsb
            vhf_role = vhf
        return {
            "count": count,
            "serials": serials,
            "adsb": adsb_role,
            "vhf": vhf_role,
            "duplicate_serials": duplicate,
            "vhf_available": self.hardware_available(),
        }


def _rtl_serials(sysfs_path: Path) -> list[str]:
    serials: list[str] = []
    try:
        devices = list(sysfs_path.iterdir())
    except OSError:
        return serials
    for device in devices:
        try:
            vendor = (device / "idVendor").read_text(encoding="ascii").strip()
            product = (device / "idProduct").read_text(encoding="ascii").strip()
        except OSError:
            continue
        if vendor != "0bda" or product not in {"2832", "2838"}:
            continue
        try:
            serial = (device / "serial").read_text(encoding="ascii").strip()
        except OSError:
            serial = ""
        serials.append(serial or "-")
    return serials
