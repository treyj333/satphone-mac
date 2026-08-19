import unittest

from satphone.models import SyncPhase
from satphone.satellite import (
    SyncMonitor,
    classify_sync_status,
    set_ntn_transport,
    status_tags,
    sync_status_is_active,
    transport_supports_ntn,
)
from tests.fakes import FakeClock, ScriptedClient, request_key


STATUS_REQUEST = {"req": "hub.sync.status"}


class StatusTests(unittest.TestCase):
    def test_extracts_stable_brace_codes(self):
        self.assertEqual(
            status_tags("waiting {network-down} {device-delay-5}"),
            {"network-down", "device-delay-5"},
        )

    def test_classifies_structured_states(self):
        self.assertEqual(
            classify_sync_status({"status": "starting {sync-begin}", "requested": 1}),
            SyncPhase.SYNCHRONIZING,
        )
        self.assertEqual(
            classify_sync_status({"status": "done {sync-end}"}),
            SyncPhase.COMPLETED,
        )
        self.assertEqual(
            classify_sync_status({"err": "no project {product-noexist}"}),
            SyncPhase.FAILED,
        )
        self.assertEqual(
            classify_sync_status({"status": "nothing changed {no-changes}"}),
            SyncPhase.COMPLETED,
        )
        self.assertEqual(
            classify_sync_status(
                {"status": "ended with error {sync-end} {sync-error}"}
            ),
            SyncPhase.FAILED,
        )
        self.assertEqual(
            classify_sync_status({"status": "done {sync-end}", "alert": True}),
            SyncPhase.FAILED,
        )

    def test_official_terminal_sync_errors_are_failures(self):
        for tag in (
            "network-timeout",
            "receive-timeout",
            "registration-failure",
            "request-failure",
            "extended-network-failure",
            "no-session",
            "hub-not-connected",
            "no-handler",
            "socket-tls-error",
        ):
            with self.subTest(tag=tag):
                self.assertEqual(
                    classify_sync_status({"status": "failure {{{}}}".format(tag)}),
                    SyncPhase.FAILED,
                )

    def test_active_is_an_explicit_conservative_inference(self):
        self.assertTrue(
            sync_status_is_active(
                {"status": "starting {connecting}", "requested": 2}
            )
        )
        self.assertTrue(
            sync_status_is_active(
                {
                    "status": "previous completion {sync-end}",
                    "requested": 0,
                    "completed": 100,
                }
            )
        )

    def test_official_sync_tag_is_an_active_synchronizing_state(self):
        response = {"sync": True, "status": "sync in progress {sync}"}
        self.assertEqual(classify_sync_status(response), SyncPhase.SYNCHRONIZING)
        self.assertTrue(sync_status_is_active(response))

    def test_official_retry_tags_are_active_waiting_states(self):
        for tag in ("auth-retry", "ticket"):
            with self.subTest(tag=tag):
                response = {
                    "alert": True,
                    "requested": 1,
                    "status": "retrying {{{}}}".format(tag),
                }
                self.assertEqual(classify_sync_status(response), SyncPhase.WAITING)
                self.assertTrue(sync_status_is_active(response))
        self.assertTrue(
            sync_status_is_active({"status": "starting {connecting}"})
        )
        self.assertFalse(
            sync_status_is_active(
                {
                    "status": "stale {connecting}",
                    "requested": 3600,
                    "completed": 10,
                }
            )
        )
        self.assertTrue(
            sync_status_is_active(
                {"status": "waiting {network-down}", "seconds": 300}
            )
        )
        self.assertFalse(sync_status_is_active({"requested": 86400}))
        self.assertTrue(
            sync_status_is_active(
                {"status": "retrying {connecting}", "alert": True}
            )
        )
        self.assertTrue(
            sync_status_is_active(
                {
                    "status": "connecting {connecting}",
                    "requested": 0,
                    "completed": 0,
                }
            )
        )

    def test_transport_methods_with_ntn(self):
        self.assertTrue(transport_supports_ntn({"method": "ntn"}))
        self.assertTrue(transport_supports_ntn({"method": "wifi-cell-ntn"}))
        self.assertFalse(transport_supports_ntn({"method": "wifi-cell"}))


class SyncMonitorTests(unittest.TestCase):
    def _monitor(self, client, timeout=10):
        client.defaults.setdefault(
            "hub.get",
            {"product": "com.example:satphone", "mode": "minimum"},
        )
        clock = FakeClock()
        return SyncMonitor(
            client,
            poll_seconds=1,
            timeout_seconds=timeout,
            clock=clock,
            sleeper=clock.sleep,
        )

    def test_does_not_mistake_old_completion_for_new_sync(self):
        baseline = {"status": "completed {sync-end}", "time": 100, "completed": 50}
        client = ScriptedClient(
            scripted={
                request_key(STATUS_REQUEST): [
                    baseline,
                    baseline,
                    {"status": "starting {sync-begin}", "requested": 2},
                    {"time": 200, "completed": 1},
                ],
                request_key({"req": "hub.sync", "out": True}): [{}],
            }
        )
        result = self._monitor(client).run("out")
        self.assertTrue(result.completed)
        self.assertTrue(result.requested)
        self.assertEqual(
            len([r for r in client.requests if r.get("req") == "hub.sync"]), 1
        )
        self.assertGreaterEqual(len(result.updates), 4)

    def test_existing_sync_is_monitored_without_duplicate_request(self):
        client = ScriptedClient(
            scripted={
                request_key(STATUS_REQUEST): [
                    {"status": "starting {connecting}", "requested": 2},
                    {"status": "done {sync-end}", "time": 200, "completed": 1},
                ]
            }
        )
        result = self._monitor(client).run("in")
        self.assertTrue(result.completed)
        self.assertFalse(result.requested)
        self.assertFalse(any(r.get("req") == "hub.sync" for r in client.requests))

    def test_official_sync_status_suppresses_duplicate_request(self):
        active = {"sync": True, "status": "sync in progress {sync}"}
        client = ScriptedClient(
            scripted={request_key(STATUS_REQUEST): [active, active]},
            defaults={"hub.sync.status": active},
        )
        result = self._monitor(client, timeout=1).run("in")
        self.assertEqual(result.phase, SyncPhase.TIMED_OUT)
        self.assertFalse(result.requested)
        self.assertFalse(any(r.get("req") == "hub.sync" for r in client.requests))

    def test_newer_request_with_lagging_completion_is_monitored(self):
        lagging = {
            "status": "previous completion {sync-end}",
            "requested": 0,
            "completed": 100,
            "time": 1000,
        }
        client = ScriptedClient(
            scripted={request_key(STATUS_REQUEST): [lagging, lagging]},
            defaults={"hub.sync.status": lagging},
        )
        result = self._monitor(client, timeout=1).run("out")
        self.assertEqual(result.phase, SyncPhase.TIMED_OUT)
        self.assertFalse(result.requested)
        self.assertFalse(any(r.get("req") == "hub.sync" for r in client.requests))

    def test_directional_sync_is_issued_exactly_once(self):
        client = ScriptedClient(
            scripted={
                request_key(STATUS_REQUEST): [
                    {},
                    {"status": "working {sync-begin}", "requested": 1},
                    {"status": "done {sync-end}", "time": 10, "completed": 0},
                ],
                request_key({"req": "hub.sync", "in": True}): [{}],
            }
        )
        result = self._monitor(client).run("in")
        self.assertTrue(result.completed)
        sync_requests = [r for r in client.requests if r.get("req") == "hub.sync"]
        self.assertEqual(sync_requests, [{"req": "hub.sync", "in": True}])

    def test_timeout_is_not_reported_as_generic_failure(self):
        old = {"status": "completed {sync-end}", "time": 100, "completed": 50}
        client = ScriptedClient(
            scripted={
                request_key(STATUS_REQUEST): [old, old, old, old],
                request_key({"req": "hub.sync", "out": True}): [{}],
            },
            defaults={"hub.sync.status": old},
        )
        result = self._monitor(client, timeout=3).run("out")
        self.assertEqual(result.phase, SyncPhase.TIMED_OUT)
        self.assertIn("local timeout", result.updates[-1].message.lower())

    def test_newly_visible_historical_time_requires_cycle_evidence(self):
        historical = {"time": 100, "completed": 50}
        client = ScriptedClient(
            scripted={
                request_key(STATUS_REQUEST): [{}, historical, historical],
                request_key({"req": "hub.sync", "out": True}): [{}],
            },
            defaults={"hub.sync.status": historical},
        )
        result = self._monitor(client, timeout=2).run("out")
        self.assertEqual(result.phase, SyncPhase.TIMED_OUT)

    def test_newly_visible_old_counter_after_active_phase_is_not_completion(self):
        client = ScriptedClient(
            scripted={
                request_key(STATUS_REQUEST): [
                    {},
                    {"status": "sync in progress {sync}"},
                    {"completed": 999999},
                ],
                request_key({"req": "hub.sync", "out": True}): [{}],
            },
            defaults={"hub.sync.status": {"completed": 1000000}},
        )
        result = self._monitor(client, timeout=3).run("out")
        self.assertEqual(result.phase, SyncPhase.TIMED_OUT)

    def test_status_failure_on_sync_request_is_not_called_accepted(self):
        client = ScriptedClient(
            scripted={
                request_key(STATUS_REQUEST): [{}],
                request_key({"req": "hub.sync", "in": True}): [
                    {"status": "no project {product-noexist}"}
                ],
            }
        )
        result = self._monitor(client).run("in")
        self.assertEqual(result.phase, SyncPhase.FAILED)
        self.assertIn("product-noexist", result.error)

    def test_empty_status_after_existing_activity_is_not_false_completion(self):
        active = {"status": "starting {connecting}", "requested": 1}
        client = ScriptedClient(
            scripted={request_key(STATUS_REQUEST): [active, {}, {}]},
            defaults={"hub.sync.status": {}},
        )
        result = self._monitor(client, timeout=2).run("in")
        self.assertEqual(result.phase, SyncPhase.TIMED_OUT)
        self.assertFalse(result.requested)
        self.assertFalse(any(r.get("req") == "hub.sync" for r in client.requests))

    def test_backward_completion_epoch_is_not_new_completion(self):
        client = ScriptedClient(
            scripted={
                request_key(STATUS_REQUEST): [
                    {"time": 200, "completed": 10},
                    {"time": 100, "completed": 11},
                ],
                request_key({"req": "hub.sync", "out": True}): [{}],
            },
            defaults={"hub.sync.status": {"time": 100, "completed": 12}},
        )
        result = self._monitor(client, timeout=2).run("out")
        self.assertEqual(result.phase, SyncPhase.TIMED_OUT)

    def test_new_no_changes_status_is_success(self):
        client = ScriptedClient(
            scripted={
                request_key(STATUS_REQUEST): [{}, {"status": "done {no-changes}"}],
                request_key({"req": "hub.sync", "in": True}): [{}],
            }
        )
        result = self._monitor(client).run("in")
        self.assertEqual(result.phase, SyncPhase.COMPLETED)
        self.assertTrue(result.requested)

    def test_old_no_changes_status_is_not_reused_as_success(self):
        old = {"status": "done {no-changes}", "time": 100, "completed": 10}
        client = ScriptedClient(
            scripted={
                request_key(STATUS_REQUEST): [old, old, old],
                request_key({"req": "hub.sync", "in": True}): [{}],
            },
            defaults={"hub.sync.status": old},
        )
        result = self._monitor(client, timeout=2).run("in")
        self.assertEqual(result.phase, SyncPhase.TIMED_OUT)

    def test_hub_off_mode_prevents_ignored_sync_request(self):
        client = ScriptedClient(
            defaults={
                "hub.get": {
                    "product": "com.example:satphone",
                    "mode": "off",
                }
            }
        )
        result = self._monitor(client).run("in")
        self.assertEqual(result.phase, SyncPhase.FAILED)
        self.assertFalse(result.requested)
        self.assertFalse(any(r.get("req") == "hub.sync" for r in client.requests))

    def test_unknown_hub_configuration_prevents_sync_request(self):
        client = ScriptedClient(defaults={"hub.get": {}})
        # Bypass the helper's normal healthy test default for this regression.
        clock = FakeClock()
        result = SyncMonitor(
            client,
            poll_seconds=1,
            timeout_seconds=2,
            clock=clock,
            sleeper=clock.sleep,
        ).run("in")
        self.assertEqual(result.phase, SyncPhase.FAILED)
        self.assertIn("ProductUID", result.error)
        self.assertFalse(any(r.get("req") == "hub.sync" for r in client.requests))

    def test_sync_status_query_error_prevents_sync_request(self):
        client = ScriptedClient(
            defaults={
                "hub.sync.status": {"err": "status unavailable {io}"},
            }
        )
        result = self._monitor(client).run("in")
        self.assertEqual(result.phase, SyncPhase.FAILED)
        self.assertFalse(result.requested)
        self.assertIn("hub.sync.status failed", result.error)
        self.assertFalse(any(r.get("req") == "hub.sync" for r in client.requests))

    def test_malformed_sync_counter_prevents_sync_request(self):
        client = ScriptedClient(
            defaults={"hub.sync.status": {"requested": True}}
        )
        result = self._monitor(client).run("in")
        self.assertEqual(result.phase, SyncPhase.FAILED)
        self.assertIn("invalid requested", result.error)
        self.assertFalse(any(r.get("req") == "hub.sync" for r in client.requests))

    def test_malformed_polled_counter_is_unresolved(self):
        client = ScriptedClient(
            scripted={
                request_key(STATUS_REQUEST): [{}, {"completed": 1.5}],
                request_key({"req": "hub.sync", "in": True}): [{}],
            }
        )
        result = self._monitor(client).run("in")
        self.assertEqual(result.phase, SyncPhase.UNRESOLVED)
        self.assertTrue(result.requested)
        self.assertIn("invalid completed", result.error)

    def test_historical_alert_does_not_block_explicit_retry(self):
        previous_failure = {
            "alert": True,
            "status": "failed {network-timeout}",
            "requested": 100,
            "time": 1000,
        }
        client = ScriptedClient(
            scripted={
                request_key(STATUS_REQUEST): [
                    previous_failure,
                    previous_failure,
                    {"status": "working {sync-begin}", "requested": 1},
                    {"status": "done {sync-end}", "time": 2000, "completed": 0},
                ],
                request_key({"req": "hub.sync", "out": True}): [{}],
            }
        )
        result = self._monitor(client).run("out")
        self.assertEqual(result.phase, SyncPhase.COMPLETED)
        self.assertTrue(result.requested)
        self.assertEqual(
            len([r for r in client.requests if r.get("req") == "hub.sync"]), 1
        )

    def test_new_failure_after_historical_failure_requires_cycle_evidence(self):
        previous_failure = {
            "alert": True,
            "status": "failed {network-timeout}",
            "requested": 100,
        }
        current_failure = dict(previous_failure, requested=1)
        client = ScriptedClient(
            scripted={
                request_key(STATUS_REQUEST): [
                    previous_failure,
                    {"status": "working {sync-begin}", "requested": 1},
                    current_failure,
                ],
                request_key({"req": "hub.sync", "in": True}): [{}],
            }
        )
        result = self._monitor(client).run("in")
        self.assertEqual(result.phase, SyncPhase.FAILED)
        self.assertTrue(result.requested)

    def test_request_age_reset_alone_is_not_completion(self):
        client = ScriptedClient(
            scripted={
                request_key(STATUS_REQUEST): [
                    {"requested": 100, "completed": 10},
                    {"requested": 0, "completed": 10},
                ],
                request_key({"req": "hub.sync", "out": True}): [{}],
            },
            defaults={"hub.sync.status": {"requested": 1, "completed": 11}},
        )
        result = self._monitor(client, timeout=2).run("out")
        self.assertEqual(result.phase, SyncPhase.TIMED_OUT)

    def test_request_age_reset_with_old_terminal_tag_is_not_completion(self):
        baseline = {
            "status": "completed {sync-end}",
            "time": 100,
            "completed": 50,
            "requested": 100,
        }
        lagging = dict(baseline, completed=51, requested=0)
        client = ScriptedClient(
            scripted={
                request_key(STATUS_REQUEST): [baseline, lagging],
                request_key({"req": "hub.sync", "out": True}): [{}],
            },
            defaults={"hub.sync.status": lagging},
        )
        result = self._monitor(client, timeout=2).run("out")
        self.assertEqual(result.phase, SyncPhase.TIMED_OUT)

    def test_observed_cycle_does_not_accept_a_provably_older_completion(self):
        baseline = {
            "status": "completed {sync-end}",
            "time": 100,
            "completed": 50,
            "requested": 100,
        }
        stale_terminal = {
            "status": "completed {sync-end}",
            "time": 100,
            "requested": 1,
            "completed": 52,
        }
        client = ScriptedClient(
            scripted={
                request_key(STATUS_REQUEST): [
                    baseline,
                    {"status": "working {sync-begin}", "requested": 0, "completed": 51},
                    stale_terminal,
                ],
                request_key({"req": "hub.sync", "out": True}): [{}],
            },
            defaults={"hub.sync.status": stale_terminal},
        )
        result = self._monitor(client, timeout=3).run("out")
        self.assertEqual(result.phase, SyncPhase.TIMED_OUT)

    def test_first_polled_no_changes_response_is_success(self):
        client = ScriptedClient(
            scripted={
                request_key(STATUS_REQUEST): [{}, {"status": "done {no-changes}"}],
                request_key({"req": "hub.sync", "in": True}): [{}],
            }
        )
        result = self._monitor(client).run("in")
        self.assertEqual(result.phase, SyncPhase.COMPLETED)

    def test_poll_sleep_never_overshoots_local_timeout(self):
        clock = FakeClock()
        client = ScriptedClient(
            scripted={request_key({"req": "hub.sync", "out": True}): [{}]},
            defaults={
                "hub.sync.status": {},
                "hub.get": {
                    "product": "com.example:satphone",
                    "mode": "minimum",
                },
            },
        )
        monitor = SyncMonitor(
            client,
            poll_seconds=10,
            timeout_seconds=1,
            clock=clock,
            sleeper=clock.sleep,
        )
        result = monitor.run("out")
        self.assertEqual(result.phase, SyncPhase.TIMED_OUT)
        self.assertEqual(clock.value, 1)

    def test_poll_transport_failure_after_request_is_unresolved(self):
        class BrokenPollClient(ScriptedClient):
            def __init__(self):
                super().__init__(
                    defaults={
                        "hub.get": {
                            "product": "com.example:satphone",
                            "mode": "minimum",
                        },
                        "hub.sync": {},
                    }
                )
                self.status_calls = 0

            def inspect(self, request):
                if request.get("req") == "hub.sync.status":
                    self.status_calls += 1
                    if self.status_calls > 1:
                        self.requests.append(dict(request))
                        raise RuntimeError("USB poll failed")
                return super().inspect(request)

        client = BrokenPollClient()
        result = self._monitor(client).run("in")
        self.assertEqual(result.phase, SyncPhase.UNRESOLVED)
        self.assertTrue(result.requested)
        self.assertIn("may still be pending", result.error)
        self.assertEqual(
            len([r for r in client.requests if r.get("req") == "hub.sync"]), 1
        )

    def test_keyboard_interrupt_during_poll_is_unresolved(self):
        class InterruptedPollClient(ScriptedClient):
            def __init__(self):
                super().__init__(defaults={"hub.sync.status": {}, "hub.sync": {}})
                self.status_calls = 0

            def inspect(self, request):
                if request.get("req") == "hub.sync.status":
                    self.status_calls += 1
                    if self.status_calls > 1:
                        self.requests.append(dict(request))
                        raise KeyboardInterrupt
                return super().inspect(request)

        result = self._monitor(InterruptedPollClient()).run("in")
        self.assertEqual(result.phase, SyncPhase.UNRESOLVED)
        self.assertTrue(result.requested)

    def test_sync_request_transport_failure_is_unresolved(self):
        class BrokenSyncRequestClient(ScriptedClient):
            def inspect(self, request):
                if request.get("req") == "hub.sync":
                    self.requests.append(dict(request))
                    raise RuntimeError("Failed to transact")
                return super().inspect(request)

        client = BrokenSyncRequestClient(defaults={"hub.sync.status": {}})
        result = self._monitor(client).run("in")
        self.assertEqual(result.phase, SyncPhase.UNRESOLVED)
        self.assertTrue(result.requested)
        self.assertIn("may have reached", result.error)
        self.assertIn("Wait until hub.sync.status", result.error)
        self.assertEqual(
            len([r for r in client.requests if r.get("req") == "hub.sync"]), 1
        )

    def test_keyboard_interrupt_during_sync_request_is_unresolved(self):
        class InterruptedSyncRequestClient(ScriptedClient):
            def inspect(self, request):
                if request.get("req") == "hub.sync":
                    self.requests.append(dict(request))
                    raise KeyboardInterrupt
                return super().inspect(request)

        client = InterruptedSyncRequestClient(defaults={"hub.sync.status": {}})
        result = self._monitor(client).run("out")
        self.assertEqual(result.phase, SyncPhase.UNRESOLVED)
        self.assertTrue(result.requested)
        self.assertEqual(
            len([r for r in client.requests if r.get("req") == "hub.sync"]), 1
        )

    def test_poll_transport_failure_while_monitoring_is_unresolved(self):
        class BrokenExistingPollClient(ScriptedClient):
            def __init__(self):
                super().__init__()
                self.status_calls = 0

            def inspect(self, request):
                if request.get("req") == "hub.sync.status":
                    self.status_calls += 1
                    if self.status_calls == 1:
                        return self._response(request) | {
                            "status": "sync in progress {sync}"
                        }
                    self.requests.append(dict(request))
                    raise RuntimeError("USB poll failed")
                return super().inspect(request)

        client = BrokenExistingPollClient()
        result = self._monitor(client).run("out")
        self.assertEqual(result.phase, SyncPhase.UNRESOLVED)
        self.assertFalse(result.requested)
        self.assertFalse(any(r.get("req") == "hub.sync" for r in client.requests))


class TransportMutationTests(unittest.TestCase):
    def test_unknown_current_transport_blocks_write(self):
        client = ScriptedClient(defaults={"card.transport": {}})
        with self.assertRaisesRegex(Exception, "no change was made"):
            set_ntn_transport(client)
        self.assertFalse(any(r.get("method") == "ntn" for r in client.requests))

    def test_transport_write_is_re_read_and_verified(self):
        query = {"req": "card.transport"}
        client = ScriptedClient(
            scripted={
                request_key(query): [
                    {"method": "wifi-cell"},
                    {"method": "ntn"},
                ],
                request_key({"req": "card.transport", "method": "ntn"}): [{}],
            }
        )
        result = set_ntn_transport(client)
        self.assertEqual(result["verified"]["method"], "ntn")

    def test_unpersisted_transport_write_is_reported(self):
        query = {"req": "card.transport"}
        client = ScriptedClient(
            scripted={
                request_key(query): [
                    {"method": "wifi-cell"},
                    {"method": "wifi-cell"},
                ],
                request_key({"req": "card.transport", "method": "ntn"}): [{}],
            }
        )
        with self.assertRaisesRegex(Exception, "was not verified"):
            set_ntn_transport(client)


if __name__ == "__main__":
    unittest.main()
