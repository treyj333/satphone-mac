import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from satphone.discord_bridge import (
    BridgeConfig,
    BridgeConfigurationError,
    DiscordToNotehubService,
    InteractionLedger,
    InteractionStatus,
    NotehubInboundClient,
    PlainMessageGuidanceLimiter,
    QueueStatus,
    _keychain_write,
    load_secret,
)


class BridgeConfigTests(unittest.TestCase):
    def test_loads_non_secret_config_and_resolves_state_path(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "bridge.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project_uid": "app:project",
                        "device_uid": "dev:device",
                        "discord_guild_id": "123",
                        "discord_channel_id": 456,
                        "allowed_user_ids": ["789"],
                        "state_path": "state/bridge.sqlite3",
                    }
                ),
                encoding="utf-8",
            )
            config = BridgeConfig.load(config_path)
            self.assertEqual(config.discord_guild_id, 123)
            self.assertEqual(config.discord_channel_id, 456)
            self.assertEqual(config.allowed_user_ids, frozenset({789}))
            self.assertEqual(
                config.state_path, (Path(directory) / "state/bridge.sqlite3").resolve()
            )

    def test_rejects_wrong_notefile_and_malformed_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.json"
            base = {
                "project_uid": "app:project",
                "device_uid": "dev:device",
                "discord_guild_id": 123,
                "discord_channel_id": 456,
                "trust_discord_command_permissions": True,
            }
            for update in (
                {"notefile": "other.qi"},
                {"discord_guild_id": True},
                {"discord_guild_id": 123.9},
                {"discord_channel_id": "1e6"},
                {"discord_channel_id": 0},
                {"discord_channel_id": 2**64},
                {"allowed_user_ids": [30.7]},
            ):
                with self.subTest(update=update):
                    path.write_text(json.dumps({**base, **update}), encoding="utf-8")
                    with self.assertRaises(BridgeConfigurationError):
                        BridgeConfig.load(path)

    def test_requires_a_local_allowlist_or_explicit_discord_trust(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.json"
            base = {
                "project_uid": "app:project",
                "device_uid": "dev:device",
                "discord_guild_id": 123,
                "discord_channel_id": 456,
                "allowed_user_ids": [],
            }
            path.write_text(json.dumps(base), encoding="utf-8")
            with self.assertRaises(BridgeConfigurationError):
                BridgeConfig.load(path)
            path.write_text(
                json.dumps(
                    {**base, "trust_discord_command_permissions": True}
                ),
                encoding="utf-8",
            )
            self.assertTrue(
                BridgeConfig.load(path).trust_discord_command_permissions
            )


class NotehubInboundClientTests(unittest.TestCase):
    def test_posts_exact_qi_envelope_once_without_leaking_token(self):
        calls = []

        def transport(path, body, headers, timeout):
            calls.append((path, body, headers, timeout))
            return 200, b"{}"

        client = NotehubInboundClient(
            "app:project", "dev:device", "top-secret", transport=transport
        )
        result = client.enqueue("hello")
        self.assertEqual(result.status, QueueStatus.QUEUED)
        self.assertEqual(len(calls), 1)
        path, body, headers, _ = calls[0]
        self.assertEqual(
            path,
            "/v1/projects/app%3Aproject/devices/dev%3Adevice/notes/messages.qi",
        )
        self.assertEqual(json.loads(body), {"body": {"msg": "hello"}})
        self.assertEqual(headers["Authorization"], "Bearer top-secret")

    def test_classifies_rejection_and_uncertain_results_without_retry(self):
        cases = (
            ((403, b'{"err":"forbidden"}'), QueueStatus.REJECTED),
            ((429, b"rate limited"), QueueStatus.REJECTED),
            ((500, b"server error"), QueueStatus.UNCERTAIN),
            ((200, b'{"unexpected":true}'), QueueStatus.UNCERTAIN),
        )
        for response, expected in cases:
            with self.subTest(response=response):
                calls = []

                def transport(path, body, headers, timeout):
                    calls.append(path)
                    return response

                result = NotehubInboundClient(
                    "app:p", "dev:d", "secret", transport=transport
                ).enqueue("hello")
                self.assertEqual(result.status, expected)
                self.assertEqual(len(calls), 1)

    def test_transport_exception_is_uncertain_and_not_retried(self):
        calls = []

        def transport(path, body, headers, timeout):
            calls.append(path)
            raise TimeoutError("lost response")

        result = NotehubInboundClient(
            "app:p", "dev:d", "secret", transport=transport
        ).enqueue("hello")
        self.assertEqual(result.status, QueueStatus.UNCERTAIN)
        self.assertEqual(len(calls), 1)


class DiscordToNotehubServiceTests(unittest.TestCase):
    def _service(self, directory, responses=None, allowed_users=frozenset()):
        calls = []
        responses = list(responses or [(200, b"{}")])

        def transport(path, body, headers, timeout):
            calls.append(json.loads(body))
            return responses.pop(0)

        config = BridgeConfig(
            project_uid="app:p",
            device_uid="dev:d",
            discord_guild_id=10,
            discord_channel_id=20,
            state_path=Path(directory) / "bridge.sqlite3",
            allowed_user_ids=frozenset(allowed_users),
        )
        ledger = InteractionLedger(config.state_path)
        notehub = NotehubInboundClient(
            config.project_uid,
            config.device_uid,
            "secret",
            transport=transport,
        )
        return DiscordToNotehubService(config, ledger, notehub), calls, config

    def test_authorization_and_utf8_limits_fail_before_notehub(self):
        with tempfile.TemporaryDirectory() as directory:
            service, calls, _ = self._service(directory, allowed_users={30})
            cases = (
                ("1", 99, 20, 30, "hello"),
                ("2", 10, 99, 30, "hello"),
                ("3", 10, 20, 31, "hello"),
                ("4", 10, 20, 30, " "),
                ("5", 10, 20, 30, "é" * 81),
            )
            for case in cases:
                with self.subTest(case=case):
                    reply = service.queue_from_interaction(*case)
                    self.assertEqual(reply.status, InteractionStatus.REJECTED)
            self.assertEqual(calls, [])

    def test_success_is_deduplicated_across_service_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            service, calls, config = self._service(directory)
            first = service.queue_from_interaction("abc", 10, 20, 30, "hello")
            self.assertEqual(first.status, InteractionStatus.QUEUED)
            self.assertEqual(len(calls), 1)

            second_notehub = NotehubInboundClient(
                "app:p",
                "dev:d",
                "secret",
                transport=lambda *args: self.fail("duplicate Notehub request"),
            )
            restarted = DiscordToNotehubService(
                config, InteractionLedger(config.state_path), second_notehub
            )
            second = restarted.queue_from_interaction("abc", 10, 20, 30, "hello")
            self.assertEqual(second.status, InteractionStatus.QUEUED)
            self.assertIn("no duplicate", second.text)

    def test_success_notifies_the_app_once_after_ledger_is_final(self):
        with tempfile.TemporaryDirectory() as directory:
            service, calls, config = self._service(directory)
            queued = []
            service.queued_callback = queued.append
            reply = service.queue_from_interaction("abc", 10, 20, 30, "hello")
            self.assertEqual(reply.status, InteractionStatus.QUEUED)
            self.assertEqual(queued, ["hello"])
            with sqlite3.connect(str(config.state_path)) as connection:
                status = connection.execute(
                    "SELECT status FROM discord_interactions WHERE interaction_id='abc'"
                ).fetchone()[0]
            self.assertEqual(status, InteractionStatus.QUEUED.value)
            replay = service.queue_from_interaction("abc", 10, 20, 30, "hello")
            self.assertEqual(replay.status, InteractionStatus.QUEUED)
            self.assertEqual(queued, ["hello"])
            self.assertEqual(len(calls), 1)

    def test_pending_and_changed_duplicate_never_send(self):
        with tempfile.TemporaryDirectory() as directory:
            service, calls, config = self._service(directory)
            ledger = InteractionLedger(config.state_path)
            ledger.reserve(
                "abc",
                30,
                __import__("hashlib").sha256(b"hello").hexdigest(),
            )
            pending = service.queue_from_interaction("abc", 10, 20, 30, "hello")
            changed = service.queue_from_interaction("abc", 10, 20, 30, "different")
            self.assertEqual(pending.status, InteractionStatus.UNCERTAIN)
            self.assertEqual(changed.status, InteractionStatus.UNCERTAIN)
            self.assertEqual(calls, [])

    def test_rate_limit_is_persistent_and_stops_before_notehub(self):
        with tempfile.TemporaryDirectory() as directory:
            service, calls, config = self._service(
                directory,
                responses=[(200, b"{}")] * 5,
            )
            for index in range(5):
                reply = service.queue_from_interaction(
                    str(index), 10, 20, 30, "message {}".format(index)
                )
                self.assertEqual(reply.status, InteractionStatus.QUEUED)
            blocked = service.queue_from_interaction(
                "sixth", 10, 20, 30, "one too many"
            )
            self.assertEqual(blocked.status, InteractionStatus.REJECTED)
            self.assertIn("Rate limit", blocked.text)
            self.assertEqual(len(calls), 5)
            with sqlite3.connect(str(config.state_path)) as connection:
                connection.execute(
                    """
                    UPDATE discord_interactions
                    SET created_at = 0
                    WHERE interaction_id != 'sixth'
                    """
                )
            replay = service.queue_from_interaction(
                "sixth", 10, 20, 30, "one too many"
            )
            self.assertEqual(replay.status, InteractionStatus.REJECTED)
            self.assertEqual(len(calls), 5)

    def test_locally_blocked_ids_have_a_short_bounded_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            service, calls, config = self._service(
                directory,
                responses=[(200, b"{}")] * 5,
            )
            for index in range(5):
                service.queue_from_interaction(
                    str(index), 10, 20, 30, "message {}".format(index)
                )
            service.queue_from_interaction(
                "old-blocked", 10, 20, 30, "rate blocked"
            )
            with sqlite3.connect(str(config.state_path)) as connection:
                connection.execute(
                    """
                    UPDATE discord_interactions
                    SET created_at = created_at - 3600
                    WHERE interaction_id = 'old-blocked'
                    """
                )
            service.queue_from_interaction(
                "new-blocked", 10, 20, 30, "still rate blocked"
            )
            with sqlite3.connect(str(config.state_path)) as connection:
                ids = {
                    row[0]
                    for row in connection.execute(
                        "SELECT interaction_id FROM discord_interactions"
                    )
                }
            self.assertNotIn("old-blocked", ids)
            self.assertIn("new-blocked", ids)
            self.assertEqual(len(calls), 5)

    def test_device_wide_rate_limit_applies_across_users(self):
        with tempfile.TemporaryDirectory() as directory:
            service, calls, _ = self._service(
                directory,
                responses=[(200, b"{}")] * 5,
            )
            for index in range(5):
                reply = service.queue_from_interaction(
                    str(index), 10, 20, 100 + index, "message {}".format(index)
                )
                self.assertEqual(reply.status, InteractionStatus.QUEUED)
            blocked = service.queue_from_interaction(
                "sixth", 10, 20, 999, "device limit"
            )
            self.assertEqual(blocked.status, InteractionStatus.REJECTED)
            self.assertEqual(len(calls), 5)

    def test_notehub_http_rejections_still_count_toward_rate_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            service, calls, _ = self._service(
                directory,
                responses=[(403, b'{"err":"forbidden"}')] * 5,
            )
            for index in range(5):
                reply = service.queue_from_interaction(
                    str(index), 10, 20, 30, "rejected {}".format(index)
                )
                self.assertEqual(reply.status, InteractionStatus.REJECTED)
            blocked = service.queue_from_interaction(
                "sixth", 10, 20, 30, "must not reach Notehub"
            )
            self.assertEqual(blocked.status, InteractionStatus.REJECTED)
            self.assertIn("Rate limit", blocked.text)
            self.assertEqual(len(calls), 5)

    def test_recent_same_message_requires_explicit_duplicate_override(self):
        with tempfile.TemporaryDirectory() as directory:
            service, calls, _ = self._service(
                directory,
                responses=[(200, b"{}"), (200, b"{}")],
            )
            first = service.queue_from_interaction(
                "first", 10, 20, 30, "repeat me"
            )
            blocked = service.queue_from_interaction(
                "second", 10, 20, 30, "repeat me"
            )
            forced = service.queue_from_interaction(
                "third", 10, 20, 30, "repeat me", True
            )
            self.assertEqual(first.status, InteractionStatus.QUEUED)
            self.assertEqual(blocked.status, InteractionStatus.REJECTED)
            self.assertIn("identical message", blocked.text)
            self.assertEqual(forced.status, InteractionStatus.QUEUED)
            self.assertEqual(len(calls), 2)

    def test_ledger_stores_hash_not_message_text(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, config = self._service(directory)
            service.queue_from_interaction("abc", 10, 20, 30, "private message")
            with sqlite3.connect(str(config.state_path)) as connection:
                dump = " ".join(
                    str(value)
                    for row in connection.execute("SELECT * FROM discord_interactions")
                    for value in row
                )
            self.assertNotIn("private message", dump)

    def test_success_reply_names_the_required_satphone_action(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, _ = self._service(directory)
            reply = service.queue_from_interaction("abc", 10, 20, 30, "hello")
            self.assertEqual(reply.status, InteractionStatus.QUEUED)
            self.assertIn("Auto receive", reply.text)
            self.assertIn("Receive Now", reply.text)
            self.assertIn("not a delivery confirmation", reply.text.lower())


class PlainMessageGuidanceTests(unittest.TestCase):
    def _config(self):
        return BridgeConfig(
            project_uid="app:p",
            device_uid="dev:d",
            discord_guild_id=10,
            discord_channel_id=20,
            state_path=Path("unused.sqlite3"),
            allowed_user_ids=frozenset({30}),
        )

    def test_only_plain_messages_in_the_configured_channel_get_guidance(self):
        limiter = PlainMessageGuidanceLimiter(self._config(), clock=lambda: 100.0)
        self.assertFalse(limiter.should_reply(99, 20, 30, False))
        self.assertFalse(limiter.should_reply(10, 99, 30, False))
        self.assertFalse(limiter.should_reply(10, 20, 30, True))
        self.assertTrue(limiter.should_reply(10, 20, 30, False))

    def test_guidance_is_rate_limited_per_user(self):
        current = [100.0]
        limiter = PlainMessageGuidanceLimiter(
            self._config(), cooldown_seconds=300, clock=lambda: current[0]
        )
        self.assertTrue(limiter.should_reply(10, 20, 30, False))
        self.assertFalse(limiter.should_reply(10, 20, 30, False))
        self.assertTrue(limiter.should_reply(10, 20, 31, False))
        current[0] += 301
        self.assertTrue(limiter.should_reply(10, 20, 30, False))


class SecretTests(unittest.TestCase):
    def test_environment_secret_takes_precedence_without_output(self):
        with patch.dict("os.environ", {"TEST_SECRET": "value"}, clear=False), patch(
            "satphone.discord_bridge._keychain_read"
        ) as keychain:
            self.assertEqual(load_secret("TEST_SECRET", "unused"), "value")
            keychain.assert_not_called()

    def test_keychain_write_never_places_secret_in_process_arguments(self):
        with patch("satphone.discord_bridge.subprocess.run") as run:
            run.return_value.returncode = 0
            _keychain_write("test-service", "private-token")
        arguments = run.call_args.args[0]
        options = run.call_args.kwargs
        self.assertEqual(arguments[-1], "-w")
        self.assertNotIn("private-token", arguments)
        self.assertEqual(options["input"], "private-token\nprivate-token\n")
        self.assertTrue(options["start_new_session"])


if __name__ == "__main__":
    unittest.main()
