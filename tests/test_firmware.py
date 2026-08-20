import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from satphone.firmware import (
    FirmwareError,
    flash_tdeck,
    list_firmware_ports,
    validate_flash_address,
)
from satphone.notecard import BLUES_USB_VID, SerialDevice


class FirmwareTests(unittest.TestCase):
    def test_notecard_port_is_never_offered_as_firmware_target(self):
        devices = [
            SerialDevice("/dev/cu.usbmodemNOTE1", "Notecard", "Blues", BLUES_USB_VID, 1),
            SerialDevice("/dev/cu.usbmodemTDECK", "ESP32-S3", "Espressif", 0x303A, 1),
            SerialDevice("/dev/tty.usbmodemTDECK", "ESP32-S3", "Espressif", 0x303A, 1),
        ]
        ports = list_firmware_ports(devices)
        self.assertEqual([port.device for port in ports], ["/dev/cu.usbmodemTDECK"])
        self.assertTrue(ports[0].likely_tdeck)

    def test_flash_address_must_be_bounded_and_aligned(self):
        self.assertEqual(validate_flash_address("0x10000"), 0x10000)
        for invalid in ("bad", "-1", "0x10001", "0x2000000"):
            with self.assertRaises(FirmwareError):
                validate_flash_address(invalid)

    def test_flash_uses_explicit_esp32s3_port_address_and_file(self):
        with tempfile.TemporaryDirectory() as directory:
            firmware = Path(directory) / "merged.bin"
            firmware.write_bytes(b"firmware")
            with patch("satphone.firmware._run_esptool", return_value="verified") as run:
                result = flash_tdeck(
                    "/dev/cu.usbmodemTDECK", firmware, "0x0", 460800
                )
            self.assertEqual(result, "verified")
            args = run.call_args.args[0]
            self.assertIn("esp32s3", args)
            self.assertIn("/dev/cu.usbmodemTDECK", args)
            self.assertEqual(args[-2], "0x0")
            self.assertEqual(args[-1], str(firmware.resolve()))


if __name__ == "__main__":
    unittest.main()
