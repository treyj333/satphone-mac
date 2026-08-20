import unittest

from satphone.diagnostics import DiagnosticRunner
from satphone.models import CheckLevel
from tests.fakes import ScriptedClient, request_key


def healthy_client():
    qi_request = {
        "req": "note.template",
        "file": "messages.qi",
        "verify": True,
    }
    qo_request = {
        "req": "note.template",
        "file": "messages.qo",
        "verify": True,
    }
    return ScriptedClient(
        scripted={
            request_key(qi_request): [
                {
                    "template": True,
                    "format": "compact",
                    "port": 56,
                    "body": {"msg": "-"},
                }
            ],
            request_key(qo_request): [
                {
                    "template": True,
                    "format": "compact",
                    "port": 57,
                    "body": {"msg": "-"},
                }
            ],
        },
        defaults={
            "card.version": {"version": "11.0"},
            "ntn.status": {"status": "{ntn-idle}"},
            "card.transport": {"method": "ntn"},
            "card.location": {
                "lat": 34.1,
                "lon": -82.2,
                "time": 100,
                "status": "GPS updated {gps}",
                "mode": "periodic",
            },
            "card.location.mode": {"mode": "periodic", "seconds": 3600},
            "ntn.gps": {"off": True},
            "hub.get": {"product": "com.example:satphone", "mode": "minimum"},
            "hub.status": {"status": "disconnected"},
            "hub.sync.status": {"time": 100, "completed": 20},
            "file.changes": {
                "total": 0,
                "info": {"messages.qi": {"changes": 0, "total": 0}},
            },
        },
    )


class DiagnosticTests(unittest.TestCase):
    def test_diagnostic_is_read_only_and_distinguishes_normal_idle_states(self):
        client = healthy_client()
        snapshot, templates = DiagnosticRunner(client).run()
        self.assertTrue(templates["messages.qi"].valid)
        self.assertTrue(templates["messages.qo"].valid)
        by_name = {check.name: check for check in snapshot.checks}
        self.assertEqual(by_name["StarNote"].level, CheckLevel.PASS)
        self.assertEqual(by_name["Notehub connection"].level, CheckLevel.INFO)
        self.assertEqual(by_name["Incoming messages"].level, CheckLevel.INFO)
        self.assertFalse(
            any(
                request.get("req") == "hub.sync"
                or request.get("cmd") == "hub.sync"
                for request in client.requests
            )
        )
        self.assertFalse(any(request.get("delete") for request in client.requests))
        for request in client.requests:
            if request.get("req") == "note.template":
                self.assertTrue(request.get("verify"))

    def test_empty_card_version_is_not_reported_as_a_connected_notecard(self):
        client = healthy_client()
        client.defaults["card.version"] = {}
        snapshot, _ = DiagnosticRunner(client).run()
        by_name = {check.name: check for check in snapshot.checks}
        self.assertEqual(by_name["Notecard"].level, CheckLevel.FAIL)
        self.assertIn("firmware version", by_name["Notecard"].summary)

    def test_diagnostic_reports_specific_hardware_location_and_sync_failures(self):
        client = healthy_client()
        client.defaults.update(
            {
                "ntn.status": {"err": "no NTN module {no-ntn-module}"},
                "card.transport": {"method": "wifi"},
                "card.location": {"status": "GPS unavailable", "count": 3},
                "hub.sync.status": {
                    "status": "opening Notehub {product-noexist} {notehub-open-failure}"
                },
            }
        )
        snapshot, _ = DiagnosticRunner(client).run()
        by_name = {check.name: check for check in snapshot.checks}
        self.assertIn("No NTN module", by_name["StarNote"].summary)
        self.assertIn("does not include NTN", by_name["Transport"].summary)
        self.assertIn("No usable", by_name["Location"].summary)
        self.assertIn("product-noexist", by_name["Last sync"].summary)

    def test_unknown_starnote_location_is_separate_warning(self):
        client = healthy_client()
        client.defaults["ntn.status"] = {
            "status": "{ntn-idle}{ntn-unknown-location}"
        }
        snapshot, _ = DiagnosticRunner(client).run()
        checks = [check for check in snapshot.checks if check.name == "StarNote location"]
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].level, CheckLevel.WARN)

    def test_documented_starnote_transfer_and_gps_states_are_active(self):
        for tag in (
            "ntn-uplinking",
            "ntn-downlinking",
            "ntn-enabling-gps",
            "ntn-disabling-gps",
        ):
            with self.subTest(tag=tag):
                client = healthy_client()
                client.defaults["ntn.status"] = {"status": "{{{}}}".format(tag)}
                snapshot, _ = DiagnosticRunner(client).run()
                by_name = {check.name: check for check in snapshot.checks}
                self.assertEqual(by_name["StarNote"].level, CheckLevel.INFO)

    def test_starnote_connect_failure_status_is_a_failure(self):
        client = healthy_client()
        client.defaults["ntn.status"] = {
            "status": "could not connect {ntn-connect-failure}"
        }
        snapshot, _ = DiagnosticRunner(client).run()
        by_name = {check.name: check for check in snapshot.checks}
        self.assertEqual(by_name["StarNote"].level, CheckLevel.FAIL)

    def test_location_configuration_query_errors_are_explicit(self):
        client = healthy_client()
        client.defaults["card.location.mode"] = {"err": "read failed {io}"}
        client.defaults["ntn.gps"] = {"err": "read failed {io}"}
        snapshot, _ = DiagnosticRunner(client).run()
        by_name = {check.name: check for check in snapshot.checks}
        self.assertEqual(by_name["Location configuration"].level, CheckLevel.FAIL)
        self.assertIn("card.location.mode failed", by_name["Location configuration"].summary)

    def test_hub_off_mode_is_blocking_configuration_failure(self):
        client = healthy_client()
        client.defaults["hub.get"] = {
            "product": "com.example:satphone",
            "mode": "off",
        }
        snapshot, _ = DiagnosticRunner(client).run()
        by_name = {check.name: check for check in snapshot.checks}
        self.assertEqual(by_name["Notehub configuration"].level, CheckLevel.FAIL)
        self.assertIn("ignores manual hub.sync", by_name["Notehub configuration"].summary)

    def test_alert_true_is_a_sync_failure_even_with_completion_tag(self):
        client = healthy_client()
        client.defaults["hub.sync.status"] = {
            "status": "ended {sync-end}",
            "alert": True,
            "time": 100,
        }
        snapshot, _ = DiagnosticRunner(client).run()
        by_name = {check.name: check for check in snapshot.checks}
        self.assertEqual(by_name["Last sync"].level, CheckLevel.FAIL)

    def test_alert_from_previous_sync_does_not_hide_active_auth_retry(self):
        client = healthy_client()
        client.defaults["hub.sync.status"] = {
            "alert": True,
            "status": "authentication retry {auth-retry}",
            "requested": 1,
        }
        snapshot, _ = DiagnosticRunner(client).run()
        by_name = {check.name: check for check in snapshot.checks}
        self.assertEqual(by_name["Last sync"].level, CheckLevel.INFO)
        self.assertIn("pending or active", by_name["Last sync"].summary)

    def test_sync_true_warns_that_work_remains(self):
        client = healthy_client()
        client.defaults["hub.sync.status"] = {
            "time": 100,
            "completed": 20,
            "sync": True,
        }
        snapshot, _ = DiagnosticRunner(client).run()
        by_name = {check.name: check for check in snapshot.checks}
        self.assertEqual(by_name["Last sync"].level, CheckLevel.WARN)
        self.assertIn("unsynchronized work remains", by_name["Last sync"].summary)

    def test_boolean_sync_counters_are_not_completion_evidence(self):
        client = healthy_client()
        client.defaults["hub.sync.status"] = {
            "time": True,
            "completed": True,
        }
        snapshot, _ = DiagnosticRunner(client).run()
        by_name = {check.name: check for check in snapshot.checks}
        self.assertEqual(by_name["Last sync"].level, CheckLevel.FAIL)
        self.assertIn("invalid", by_name["Last sync"].summary)

    def test_template_transport_error_is_not_presented_as_repairable(self):
        client = healthy_client()
        request = {"req": "note.template", "file": "messages.qi", "verify": True}
        client.scripted[request_key(request)].clear()
        client.scripted[request_key(request)].append({"err": "USB failed {io}"})
        snapshot, templates = DiagnosticRunner(client).run()
        by_name = {check.name: check for check in snapshot.checks}
        self.assertFalse(templates["messages.qi"].repairable)
        self.assertEqual(by_name["messages.qi template"].level, CheckLevel.FAIL)
        self.assertIn("could not be verified", by_name["messages.qi template"].summary)


if __name__ == "__main__":
    unittest.main()
