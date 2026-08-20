"""Explicit, T-Deck-first ESP32-S3 inspection and firmware flashing."""

from __future__ import annotations

import contextlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from .notecard import BLUES_USB_VID, SerialDevice, list_serial_devices


ESPRESSIF_USB_VID = 0x303A
COMMON_USB_SERIAL_VIDS = {ESPRESSIF_USB_VID, 0x1A86, 0x10C4, 0x0403}
MAX_FIRMWARE_BYTES = 32 * 1024 * 1024


class FirmwareError(RuntimeError):
    pass


@dataclass(frozen=True)
class FirmwarePort:
    device: str
    description: str
    likely_tdeck: bool


def list_firmware_ports(devices: Optional[List[SerialDevice]] = None) -> List[FirmwarePort]:
    """List non-Notecard serial devices, preferring common ESP USB adapters."""
    values = devices if devices is not None else list_serial_devices()
    ports: List[FirmwarePort] = []
    for device in values:
        if device.vid == BLUES_USB_VID:
            continue
        haystack = "{} {} {}".format(
            device.device, device.description, device.manufacturer
        ).lower()
        likely = device.vid in COMMON_USB_SERIAL_VIDS or any(
            marker in haystack
            for marker in ("esp32", "espressif", "cp210", "ch340", "ch910", "ftdi", "usbserial")
        )
        if device.device.startswith("/dev/cu."):
            ports.append(FirmwarePort(device.device, device.description, likely))
    return sorted(ports, key=lambda item: (not item.likely_tdeck, item.device))


def validate_flash_address(value: str) -> int:
    try:
        address = int(str(value).strip(), 0)
    except ValueError as exc:
        raise FirmwareError("Flash address must be a number such as 0x0 or 0x10000.") from exc
    if address < 0 or address > 0x1FFFFFF:
        raise FirmwareError("Flash address is outside the supported ESP32 range.")
    if address % 0x1000 != 0:
        raise FirmwareError("Flash address must be aligned to a 0x1000-byte boundary.")
    return address


def validate_firmware_file(path: Path) -> Path:
    file_path = Path(path).expanduser().resolve()
    if file_path.suffix.lower() != ".bin":
        raise FirmwareError("Choose an ESP32 .bin firmware image.")
    if not file_path.is_file():
        raise FirmwareError("The selected firmware file does not exist.")
    size = file_path.stat().st_size
    if size <= 0 or size > MAX_FIRMWARE_BYTES:
        raise FirmwareError("The firmware image has an invalid size.")
    return file_path


def _run_esptool(arguments: List[str]) -> str:
    try:
        import esptool
    except ImportError as exc:
        raise FirmwareError("The bundled ESP flashing tool is missing.") from exc
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            esptool.main(arguments)
    except SystemExit as exc:
        if exc.code not in (None, 0):
            raise FirmwareError(output.getvalue().strip() or "ESP flashing tool failed.") from exc
    except Exception as exc:
        detail = output.getvalue().strip()
        raise FirmwareError(detail or str(exc)) from exc
    return output.getvalue().strip()


def inspect_tdeck(port: str) -> str:
    if not str(port).startswith("/dev/cu."):
        raise FirmwareError("Choose a macOS call-out serial port.")
    return _run_esptool(["--port", str(port), "chip-id"])


def flash_tdeck(
    port: str,
    firmware_path: Path,
    address_text: str = "0x0",
    baud: int = 460800,
    progress: Optional[Callable[[str], None]] = None,
) -> str:
    """Flash one explicitly selected ESP32-S3 binary at its documented offset."""
    if not str(port).startswith("/dev/cu."):
        raise FirmwareError("Choose a macOS call-out serial port.")
    file_path = validate_firmware_file(firmware_path)
    address = validate_flash_address(address_text)
    if baud not in (115200, 230400, 460800, 921600):
        raise FirmwareError("Choose a supported flash speed.")
    if progress:
        progress("Connecting to the T-Deck ESP32-S3…")
    output = _run_esptool(
        [
            "--chip",
            "esp32s3",
            "--port",
            str(port),
            "--baud",
            str(baud),
            "--before",
            "default-reset",
            "--after",
            "hard-reset",
            "write-flash",
            hex(address),
            str(file_path),
        ]
    )
    if progress:
        progress("Firmware written and verified; the T-Deck was reset.")
    return output
