import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from satphone.debug import capture_trace
from satphone.notecard import SatphoneError, UnsafeSerialStateError


class FakeSerial:
    def __init__(self):
        self.timeout = 10
        self.writes = []
        self.lines = [b"ntn: searching\n"]

    def write(self, value):
        self.writes.append(value)

    def flush(self):
        pass

    def readline(self):
        return self.lines.pop(0) if self.lines else b""

    def reset_input_buffer(self):
        pass


class TraceClient:
    def __init__(self, fail_resync=False, fail_enable=False):
        self.device = "/dev/cu.test"
        self.serial_port = FakeSerial()
        self.requests = []
        self.resynced = False
        self.fail_resync = fail_resync
        self.fail_enable = fail_enable
        self.closed = False

    def request(self, request):
        self.requests.append(request)
        if self.fail_enable:
            raise RuntimeError("trace acknowledgement lost")
        return {}

    def resynchronize_serial(self):
        if self.fail_resync:
            raise RuntimeError("not quiet")
        self.resynced = True

    def close(self):
        self.closed = True


class DebugTests(unittest.TestCase):
    def test_trace_is_exclusive_logged_and_disabled(self):
        client = TraceClient()
        with tempfile.TemporaryDirectory() as directory, patch(
            "satphone.debug.time.monotonic", side_effect=[0, 0, 2]
        ), patch("satphone.debug.time.sleep"):
            path = capture_trace(
                client,
                duration_seconds=1,
                sync_direction="in",
                logs_dir=Path(directory),
            )
            content = path.read_text(encoding="utf-8")
        self.assertEqual(
            client.requests, [{"req": "card.trace", "mode": "on"}]
        )
        raw = b"".join(client.serial_port.writes)
        self.assertIn(b'"cmd":"hub.sync","in":true', raw)
        self.assertIn(b'"cmd":"card.trace","mode":"off"', raw)
        self.assertIn("ntn: searching", content)
        self.assertTrue(client.resynced)

    def test_trace_cleanup_failure_is_never_silent(self):
        client = TraceClient(fail_resync=True)
        with tempfile.TemporaryDirectory() as directory, patch(
            "satphone.debug.time.monotonic", side_effect=[0, 2]
        ), patch("satphone.debug.time.sleep"):
            with self.assertRaisesRegex(SatphoneError, "cleanup could not be confirmed"):
                capture_trace(
                    client,
                    duration_seconds=1,
                    logs_dir=Path(directory),
                )
        self.assertTrue(client.closed)

    def test_capture_and_cleanup_failures_are_reported_together(self):
        client = TraceClient(fail_resync=True)

        def broken_callback(_line):
            raise RuntimeError("display failed")

        with tempfile.TemporaryDirectory() as directory, patch(
            "satphone.debug.time.monotonic", side_effect=[0, 0]
        ), patch("satphone.debug.time.sleep"):
            with self.assertRaises(UnsafeSerialStateError) as raised:
                capture_trace(
                    client,
                    duration_seconds=1,
                    logs_dir=Path(directory),
                    line_callback=broken_callback,
                )
        self.assertIn("display failed", str(raised.exception))
        self.assertIn("resynchronization failed", str(raised.exception))
        self.assertTrue(client.closed)

    def test_lost_trace_enable_ack_still_attempts_trace_off(self):
        client = TraceClient(fail_enable=True)
        with tempfile.TemporaryDirectory() as directory, patch(
            "satphone.debug.time.sleep"
        ):
            with self.assertRaisesRegex(RuntimeError, "acknowledgement lost"):
                capture_trace(client, duration_seconds=1, logs_dir=Path(directory))
        raw = b"".join(client.serial_port.writes)
        self.assertIn(b'"cmd":"card.trace","mode":"off"', raw)


if __name__ == "__main__":
    unittest.main()
