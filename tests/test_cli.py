import contextlib
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from satphone.cli import live_debug, main, send_message
from satphone.notecard import (
    BLUES_USB_VID,
    NOTECARD_USB_PID,
    SerialDevice,
    UnsafeSerialStateError,
)
from tests.fakes import ScriptedClient
from tests.fakes import request_key


class CliBoundaryTests(unittest.TestCase):
    def test_non_finite_timing_arguments_are_rejected_before_connect(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--sync-timeout", "nan"]), 2)
            self.assertEqual(main(["--poll-seconds", "inf"]), 2)

    def test_cancelled_multi_port_selection_exits_without_traceback(self):
        devices = [
            SerialDevice(
                "/dev/cu.usbmodemNOTE1",
                "Notecard",
                "Blues",
                BLUES_USB_VID,
                NOTECARD_USB_PID,
            ),
            SerialDevice(
                "/dev/cu.usbmodemNOTE2",
                "Notecard",
                "Blues",
                BLUES_USB_VID,
                NOTECARD_USB_PID,
            ),
        ]
        with patch("satphone.cli.list_serial_devices", return_value=devices), patch(
            "builtins.input", side_effect=EOFError
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            self.assertEqual(main([]), 130)

    def test_raw_debug_unknown_hub_config_never_requests_sync(self):
        client = ScriptedClient(defaults={"hub.get": {}})
        with patch("builtins.input", side_effect=["y", "", "2"]), patch(
            "satphone.cli.capture_trace", return_value=Path("trace.txt")
        ) as capture, contextlib.redirect_stdout(io.StringIO()):
            live_debug(client)
        self.assertIsNone(capture.call_args.kwargs["sync_direction"])
        self.assertFalse(
            any(
                r.get("req") == "hub.sync" or r.get("cmd") == "hub.sync"
                for r in client.requests
            )
        )

    def test_raw_debug_sync_status_error_never_requests_sync(self):
        client = ScriptedClient(
            defaults={
                "hub.get": {
                    "product": "com.example:satphone",
                    "mode": "minimum",
                },
                "hub.sync.status": {"err": "status unavailable {io}"},
            }
        )
        with patch("builtins.input", side_effect=["y", "", "2"]), patch(
            "satphone.cli.capture_trace", return_value=Path("trace.txt")
        ) as capture, contextlib.redirect_stdout(io.StringIO()):
            live_debug(client)
        self.assertIsNone(capture.call_args.kwargs["sync_direction"])

    def test_raw_debug_rechecks_for_a_sync_started_during_confirmation(self):
        status_request = {"req": "hub.sync.status"}
        client = ScriptedClient(
            scripted={
                request_key(status_request): [
                    {"status": "idle"},
                    {"status": "sync in progress {sync}"},
                ]
            },
            defaults={
                "hub.get": {
                    "product": "com.example:satphone",
                    "mode": "minimum",
                }
            },
        )
        with patch(
            "builtins.input", side_effect=["y", "", "2", "y"]
        ), patch(
            "satphone.cli.capture_trace", return_value=Path("trace.txt")
        ) as capture, contextlib.redirect_stdout(io.StringIO()):
            live_debug(client)
        self.assertIsNone(capture.call_args.kwargs["sync_direction"])
        self.assertEqual(
            sum(1 for request in client.requests if request == status_request),
            2,
        )

    def test_unsafe_trace_cleanup_closes_client_and_returns_failure(self):
        class ClosingClient:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        client = ClosingClient()
        with patch("satphone.cli._choose_port", return_value="/dev/cu.test"), patch(
            "satphone.cli.NotecardClient.open", return_value=client
        ), patch("builtins.input", side_effect=["8", "y", "", "1"]), patch(
            "satphone.cli.capture_trace",
            side_effect=UnsafeSerialStateError("cleanup failed; unplug/replug"),
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            self.assertEqual(main([]), 1)
        self.assertTrue(client.closed)

    def test_send_transport_failure_reports_uncertain_and_never_syncs(self):
        class BrokenAddClient(ScriptedClient):
            def request(self, request, raise_on_error=True):
                if request.get("req") == "note.add":
                    self.requests.append(dict(request))
                    raise RuntimeError("Failed to transact")
                return super().request(request, raise_on_error=raise_on_error)

        client = BrokenAddClient(
            defaults={
                "note.template": {
                    "template": True,
                    "format": "compact",
                    "port": 57,
                    "body": {"msg": "-"},
                }
            }
        )
        output = io.StringIO()
        with patch("builtins.input", return_value="hello"), contextlib.redirect_stdout(
            output
        ):
            send_message(client, SimpleNamespace())
        self.assertIn("queue state is uncertain", output.getvalue().lower())
        self.assertEqual(
            len([r for r in client.requests if r.get("req") == "note.add"]), 1
        )
        self.assertFalse(any(r.get("req") == "hub.sync" for r in client.requests))


if __name__ == "__main__":
    unittest.main()
