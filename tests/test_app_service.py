"""Tests for small application-facing device operations."""

import unittest
from unittest.mock import patch

from satphone.app_service import DeviceService
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


if __name__ == "__main__":
    unittest.main()
