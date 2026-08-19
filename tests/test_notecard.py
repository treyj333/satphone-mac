import unittest
from types import SimpleNamespace
from unittest.mock import patch

from satphone.notecard import (
    BLUES_USB_VID,
    NOTECARD_USB_PID,
    NotecardClient,
    NotecardRequestError,
    SatphoneError,
    SerialDevice,
    discover_notecard_ports,
)


def device(path, vid=None, pid=None, description="USB modem", manufacturer=""):
    return SerialDevice(path, description, manufacturer, vid, pid)


class DiscoveryTests(unittest.TestCase):
    def test_prefers_exact_blues_usb_id_and_cu_node(self):
        devices = [
            device("/dev/tty.usbmodemNOTE1", BLUES_USB_VID, NOTECARD_USB_PID),
            device("/dev/cu.usbmodemNOTE1", BLUES_USB_VID, NOTECARD_USB_PID),
            device("/dev/cu.usbmodemOTHER", 0x1234, 0x9999),
        ]
        found = discover_notecard_ports(devices)
        self.assertEqual([item.device for item in found], ["/dev/cu.usbmodemNOTE1"])

    def test_uses_name_fallback(self):
        devices = [
            device("/dev/cu.usbmodemABC", description="Blues Notecard"),
            device("/dev/cu.Bluetooth-Incoming-Port"),
        ]
        found = discover_notecard_ports(devices)
        self.assertEqual([item.device for item in found], ["/dev/cu.usbmodemABC"])

    def test_generic_usbmodem_is_last_resort(self):
        devices = [device("/dev/cu.usbmodemABC"), device("/dev/cu.usbserialXYZ")]
        found = discover_notecard_ports(devices)
        self.assertEqual([item.device for item in found], ["/dev/cu.usbmodemABC"])


class FakeCard:
    def __init__(self, response):
        self.response = response

    def Transaction(self, request):
        return self.response


class FakePort:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class ClientTests(unittest.TestCase):
    def test_records_successful_transaction(self):
        client = NotecardClient(FakeCard({"version": "11.0"}), FakePort(), "test")
        response = client.request({"req": "card.version"})
        self.assertEqual(response, {"version": "11.0"})
        self.assertEqual(client.history[0]["request"], {"req": "card.version"})
        self.assertEqual(client.history[0]["response"], {"version": "11.0"})

    def test_structured_error_is_returned_by_inspect(self):
        client = NotecardClient(
            FakeCard({"err": "no module {no-ntn-module}"}), FakePort(), "test"
        )
        response = client.inspect({"req": "ntn.status"})
        self.assertIn("no-ntn-module", response["err"])

    def test_structured_error_raises_for_mutating_request(self):
        client = NotecardClient(
            FakeCard({"err": "bad template {bad-request}"}), FakePort(), "test"
        )
        with self.assertRaises(NotecardRequestError):
            client.request({"req": "note.template", "file": "messages.qi"})

    def test_missing_transaction_response_is_not_coerced_to_success(self):
        client = NotecardClient(FakeCard(None), FakePort(), "test")
        with self.assertRaisesRegex(SatphoneError, "no structured response"):
            client.inspect({"req": "hub.sync.status"})
        self.assertIn("no structured response", client.history[0]["error"])

    def test_open_rejects_empty_card_version_and_closes_port(self):
        port = FakePort()
        serial_module = SimpleNamespace(
            Serial=lambda **_kwargs: port,
            EIGHTBITS=8,
            PARITY_NONE="N",
            STOPBITS_ONE=1,
        )
        notecard_module = SimpleNamespace(
            OpenSerial=lambda _port, debug=False: FakeCard({})
        )
        with patch.dict(
            "sys.modules", {"serial": serial_module, "notecard": notecard_module}
        ):
            with self.assertRaisesRegex(SatphoneError, "card.version"):
                NotecardClient.open("/dev/cu.test")
        self.assertTrue(port.closed)


if __name__ == "__main__":
    unittest.main()
