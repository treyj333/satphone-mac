"""Lightweight structure tests for the macOS interface."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from satphone.app_service import DeviceService
from satphone.gui import APP_DISPLAY_NAME, MainWindow, _satellite_readiness_copy
from satphone.models import OperationUpdate, SyncPhase, SyncUpdate


class GuiStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_primary_workflow_is_small_and_device_aware(self) -> None:
        with tempfile.TemporaryDirectory() as support_dir:
            with patch.dict(os.environ, {"SATPHONE_APP_SUPPORT": support_dir}, clear=False):
                with patch.object(DeviceService, "ports", return_value=[]):
                    with patch.object(MainWindow, "_start_bridge"):
                        window = MainWindow()
        try:
            window.resize(1060, 760)
            window.show()
            self.app.processEvents()
            labels = [window.tabs.tabText(index) for index in range(window.tabs.count())]
            self.assertEqual(labels, ["Messages", "Device", "Tools", "Help"])
            self.assertIs(window.tabs.currentWidget(), window.messages_tab)
            self.assertEqual(window.device_tabs.count(), 3)
            self.assertEqual(window.tool_tabs.count(), 3)
            self.assertEqual(window.ready_title.text(), "Connect your device")
            self.assertEqual(window.activity_title.text(), "Activity")
            self.assertTrue(window.activity_details.isHidden())
            self.assertFalse(window.send_button.isEnabled())
            self.assertTrue(window.clear_inbox_button.isHidden())
            self.assertEqual(
                [window.device_tabs.tabText(index) for index in range(window.device_tabs.count())],
                ["Connect", "Health & Repair", "Discord & Notehub"],
            )
            self.assertGreaterEqual(window.minimumSize().width(), 900)
            self.assertGreaterEqual(window.minimumSize().height(), 700)
            self.assertGreaterEqual(window.messages_tab.layout().contentsMargins().left(), 24)
            self.assertEqual(window.windowTitle(), APP_DISPLAY_NAME)

            window.tabs.setCurrentWidget(window.device_tab)
            window.device_tabs.setCurrentWidget(window.connections_tab)
            self.app.processEvents()
            for control in (
                window.project_uid,
                window.device_uid,
                window.guild_id,
                window.channel_id,
                window.allowed_users,
                window.discord_token,
                window.notehub_token,
                window.save_connection_button,
                window.save_secrets_button,
            ):
                self.assertGreaterEqual(control.height(), 34, repr(control))

            style = window.styleSheet().lower()
            self.assertIn("background: #171717", style)
            self.assertNotIn("#155eef", style)

            with patch.object(DeviceService, "ports", return_value=[]):
                self.assertTrue(
                    window._run_worker(
                        lambda: "done",
                        label="Worker lifecycle test",
                        usb=True,
                    )
                )
                self.assertTrue(window.thread_pool.waitForDone(1000))
                self.app.processEvents()
            self.assertFalse(window.usb_busy)
            self.assertFalse(window.active_workers)
            self.assertEqual(window.activity_title.text(), "Task complete")
            self.assertTrue(window.activity_progress.isHidden())

            def progress_task(emit):
                emit(OperationUpdate("USB", "Opening the Notecard over USB."))
                emit(
                    SyncUpdate(
                        elapsed=1,
                        phase=SyncPhase.WAITING,
                        message="Waiting for satellite network.",
                        response={},
                    )
                )
                return "done"

            self.assertTrue(
                window._run_worker(
                    progress_task,
                    label="Outbound satellite message",
                    with_progress=True,
                )
            )
            self.assertTrue(window.thread_pool.waitForDone(1000))
            self.app.processEvents()
            activity_text = window.activity_details.toPlainText()
            self.assertIn("Opening the Notecard over USB.", activity_text)
            self.assertIn("Waiting for satellite network.", activity_text)
            self.assertEqual(window.activity_title.text(), "Task complete")
            self.assertFalse(window.activity_details.isHidden())
        finally:
            window.close()


class SatelliteReadinessCopyTests(unittest.TestCase):
    def test_connecting_never_claims_ready(self) -> None:
        title, detail = _satellite_readiness_copy(
            {
                "ntn": {
                    "status": "waiting for satellite network {ntn-connecting}{ntn-power}{ntn-gps}"
                },
                "transport": {"method": "ntn"},
            }
        )
        self.assertEqual(title, "Searching for satellite")
        self.assertIn("move outside", detail.lower())

    def test_idle_explains_that_coverage_is_unproven(self) -> None:
        title, detail = _satellite_readiness_copy(
            {
                "ntn": {"status": "{ntn-idle}"},
                "transport": {"method": "ntn"},
            }
        )
        self.assertEqual(title, "Satellite not active")
        self.assertIn("does not prove", detail.lower())

    def test_active_transfer_is_precise_about_delivery(self) -> None:
        title, detail = _satellite_readiness_copy(
            {
                "ntn": {"status": "{ntn-connected}{ntn-uplinking}"},
                "transport": {"method": "ntn"},
            }
        )
        self.assertEqual(title, "Satellite transfer active")
        self.assertIn("completed sync", detail.lower())

    def test_non_ntn_transport_is_not_ready(self) -> None:
        title, _detail = _satellite_readiness_copy(
            {
                "ntn": {"status": "{ntn-idle}"},
                "transport": {"method": "wifi"},
            }
        )
        self.assertEqual(title, "Satellite mode is not enabled")


if __name__ == "__main__":
    unittest.main()
