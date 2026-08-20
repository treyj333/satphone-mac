import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from satphone.app_paths import bridge_config_path, bridge_state_path, migrate_bridge_config


class AppPathTests(unittest.TestCase):
    def test_migrates_nonsecret_config_and_rehomes_state_database(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as support_dir:
            source = Path(source_dir) / "discord-bridge.json"
            source.write_text(
                json.dumps(
                    {
                        "project_uid": "app:test",
                        "device_uid": "dev:test",
                        "discord_guild_id": 123,
                        "discord_channel_id": 456,
                        "allowed_user_ids": [789],
                        "state_path": "logs/old.sqlite3",
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"SATPHONE_APP_SUPPORT": support_dir}, clear=False):
                destination = migrate_bridge_config([source])
                raw = json.loads(destination.read_text(encoding="utf-8"))
                self.assertEqual(destination, bridge_config_path())
                self.assertEqual(raw["state_path"], str(bridge_state_path()))
                self.assertEqual(raw["device_uid"], "dev:test")

    def test_existing_user_config_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as support_dir:
            source = Path(source_dir) / "discord-bridge.json"
            source.write_text('{"device_uid":"new"}', encoding="utf-8")
            with patch.dict(os.environ, {"SATPHONE_APP_SUPPORT": support_dir}, clear=False):
                destination = bridge_config_path()
                destination.write_text('{"device_uid":"keep"}', encoding="utf-8")
                migrate_bridge_config([source])
                self.assertEqual(json.loads(destination.read_text())["device_uid"], "keep")


if __name__ == "__main__":
    unittest.main()
