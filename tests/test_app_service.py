"""Tests for small application-facing device operations."""

import unittest
from unittest.mock import Mock, patch

from satphone.app_service import DeviceService
from satphone.messages import OUTBOUND_TEMPLATE
from satphone.models import (
    OperationUpdate,
    SyncPhase,
    SyncResult,
    SyncUpdate,
    TemplateCheck,
)
from tests.fakes import ScriptedClient


class ConnectionStatusTests(unittest.TestCase):
    def test_connection_status_is_read_only_and_does_not_start_sync(self) -> None:
        client = ScriptedClient(
            defaults={
                "ntn.status": {"status": "{ntn-connecting}"},
                "card.transport": {"method": "ntn"},
            }
        )
        service = DeviceService()
        with patch.object(
            service,
            "_with_client",
            side_effect=lambda _port, action: action(client),
        ):
            result = service.connection_status(client.device)

        self.assertEqual(result["ntn"]["status"], "{ntn-connecting}")
        self.assertEqual(result["transport"]["method"], "ntn")
        self.assertEqual(
            [request["req"] for request in client.requests],
            ["ntn.status", "card.transport"],
        )
        self.assertFalse(any(request.get("req") == "hub.sync" for request in client.requests))

    def test_connection_status_reports_each_safe_step(self) -> None:
        client = ScriptedClient(
            defaults={
                "ntn.status": {"status": "{ntn-idle}"},
                "card.transport": {"method": "ntn"},
            }
        )
        service = DeviceService()
        updates = []
        with patch.object(
            service,
            "_with_client",
            side_effect=lambda _port, action: action(client),
        ):
            service.connection_status(client.device, callback=updates.append)

        self.assertEqual(
            [update.stage for update in updates],
            ["USB", "StarNote", "Transport"],
        )

    def test_send_reports_queue_and_satellite_progress_without_message_text(self) -> None:
        client = ScriptedClient()
        service = DeviceService()
        updates = []
        sync_update = SyncUpdate(
            elapsed=2,
            phase=SyncPhase.WAITING,
            message="Waiting for satellite network.",
            response={},
        )
        sync_result = SyncResult(
            direction="out",
            phase=SyncPhase.TIMED_OUT,
            requested=True,
            updates=[sync_update],
            response={},
            error="Still waiting.",
        )
        monitor = Mock()
        monitor.run.side_effect = lambda _direction, callback: (
            callback(sync_update),
            sync_result,
        )[1]
        template = TemplateCheck(
            spec=OUTBOUND_TEMPLATE,
            exists=True,
            valid=True,
            response={},
        )
        secret_message = "private test payload"

        with (
            patch.object(
                service,
                "_with_client",
                side_effect=lambda _port, action: action(client),
            ),
            patch("satphone.app_service.verify_template", return_value=template),
            patch("satphone.app_service.queue_outbound_message", return_value={}),
            patch("satphone.app_service.SyncMonitor", return_value=monitor),
        ):
            service.send_message(
                client.device, secret_message, callback=updates.append
            )

        stages = [
            update.stage for update in updates if isinstance(update, OperationUpdate)
        ]
        self.assertEqual(stages, ["USB", "Setup", "Queue", "Satellite"])
        self.assertIn(sync_update, updates)
        self.assertNotIn(
            secret_message,
            " ".join(getattr(update, "message", "") for update in updates),
        )


if __name__ == "__main__":
    unittest.main()
