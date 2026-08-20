import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from satphone.location import inspect_location, set_fixed_test_location
from satphone.models import CheckLevel, DiagnosticCheck, DiagnosticSnapshot
from satphone.notecard import SatphoneError
from satphone.reporting import export_report, format_report
from tests.fakes import ScriptedClient, request_key


class LocationTests(unittest.TestCase):
    def test_location_does_not_claim_triangulation_is_gps(self):
        client = ScriptedClient(
            defaults={
                "card.location": {
                    "lat": 34.1,
                    "lon": -82.2,
                    "time": 100,
                    "status": "tower location",
                    "mode": "periodic",
                }
            }
        )
        location = inspect_location(client)
        self.assertTrue(location.known)
        self.assertEqual(location.source, "last-known or triangulated")

    def test_gps_activity_tags_are_not_location_source_proof(self):
        for tag in (
            "gps-inactive",
            "gps-starting",
            "gps-signal",
            "gps-sats",
            "gnss",
        ):
            with self.subTest(tag=tag):
                client = ScriptedClient(
                    defaults={
                        "card.location": {
                            "lat": 34.1,
                            "lon": -82.2,
                            "status": "GPS state {{{}}}".format(tag),
                            "mode": "periodic",
                        }
                    }
                )
                self.assertEqual(
                    inspect_location(client).source, "last-known or triangulated"
                )

    def test_fixed_location_sets_exact_known_coordinates_and_ntn_override(self):
        client = ScriptedClient(
            scripted={
                request_key({"req": "card.location.mode"}): [
                    {"mode": "periodic"},
                    {
                        "mode": "fixed",
                        "lat": 34.123456,
                        "lon": -82.654321,
                    },
                ],
                request_key({"req": "ntn.gps"}): [
                    {"off": True},
                    {"on": True},
                ],
            },
            defaults={
                "card.location": {
                    "mode": "off",
                    "lat": 34.123456,
                    "lon": -82.654321,
                    "status": "fixed location",
                }
            },
        )
        source_client = ScriptedClient(
            defaults={
                "card.location": {
                    "lat": 34.123456,
                    "lon": -82.654321,
                    "status": "{gps}",
                }
            }
        )
        location = inspect_location(source_client)
        set_fixed_test_location(client, location)
        self.assertEqual(
            client.requests,
            [
                {"req": "card.location.mode"},
                {"req": "ntn.gps"},
                {
                    "req": "card.location.mode",
                    "mode": "fixed",
                    "lat": 34.123456,
                    "lon": -82.654321,
                },
                {"req": "ntn.gps", "on": True},
                {"req": "card.location.mode"},
                {"req": "ntn.gps"},
                {"req": "card.location"},
            ],
        )

    def test_partial_fixed_location_failure_is_explicit(self):
        class PartialClient(ScriptedClient):
            def request(self, request, raise_on_error=True):
                self.requests.append(request)
                if request.get("req") == "ntn.gps":
                    raise RuntimeError("GPS override rejected")
                return {}

        source_client = ScriptedClient(
            defaults={"card.location": {"lat": 34.0, "lon": -82.0, "status": "{gps}"}}
        )
        client = PartialClient(
            defaults={
                "card.location.mode": {"mode": "periodic"},
                "ntn.gps": {"off": True},
            }
        )
        with self.assertRaisesRegex(SatphoneError, "partially configured"):
            set_fixed_test_location(client, inspect_location(source_client))

    def test_unknown_current_configuration_blocks_all_location_writes(self):
        source_client = ScriptedClient(
            defaults={"card.location": {"lat": 34.0, "lon": -82.0, "status": "{gps}"}}
        )
        client = ScriptedClient()
        with self.assertRaisesRegex(SatphoneError, "no change was made"):
            set_fixed_test_location(client, inspect_location(source_client))
        self.assertEqual(
            client.requests,
            [{"req": "card.location.mode"}, {"req": "ntn.gps"}],
        )

    def test_unrecognized_location_mode_blocks_all_writes(self):
        source = inspect_location(
            ScriptedClient(
                defaults={
                    "card.location": {
                        "lat": 34.0,
                        "lon": -82.0,
                        "status": "{gps}",
                    }
                }
            )
        )
        client = ScriptedClient(
            defaults={
                "card.location.mode": {"mode": "banana"},
                "ntn.gps": {"off": True},
            }
        )
        with self.assertRaisesRegex(SatphoneError, "unrecognized mode"):
            set_fixed_test_location(client, source)
        self.assertEqual(
            client.requests,
            [{"req": "card.location.mode"}, {"req": "ntn.gps"}],
        )

    def test_lost_first_location_write_ack_reports_uncertain_state(self):
        class LostModeAckClient(ScriptedClient):
            def request(self, request, raise_on_error=True):
                self.requests.append(dict(request))
                if request.get("req") == "card.location.mode":
                    raise RuntimeError("Failed to transact")
                return {}

        source = inspect_location(
            ScriptedClient(
                defaults={
                    "card.location": {
                        "lat": 34.0,
                        "lon": -82.0,
                        "status": "{gps}",
                    }
                }
            )
        )
        client = LostModeAckClient(
            defaults={
                "card.location.mode": {"mode": "periodic"},
                "ntn.gps": {"off": True},
            }
        )
        with self.assertRaisesRegex(SatphoneError, "may have applied"):
            set_fixed_test_location(client, source)
        writes = [request for request in client.requests if "mode" in request]
        self.assertEqual(len(writes), 1)
        self.assertFalse(
            any(request.get("req") == "ntn.gps" and request.get("on") for request in client.requests)
        )

    def test_triangulated_location_is_rejected_by_mutation_service(self):
        source_client = ScriptedClient(
            defaults={
                "card.location": {
                    "lat": 34.0,
                    "lon": -82.0,
                    "status": "tower location",
                }
            }
        )
        client = ScriptedClient()
        with self.assertRaisesRegex(ValueError, "tower or triangulated"):
            set_fixed_test_location(client, inspect_location(source_client))
        self.assertEqual(client.requests, [])

    def test_wrong_persisted_coordinates_fail_post_write_verification(self):
        query_mode = {"req": "card.location.mode"}
        query_gps = {"req": "ntn.gps"}
        client = ScriptedClient(
            scripted={
                request_key(query_mode): [
                    {"mode": "periodic"},
                    {"mode": "fixed", "lat": 1.0, "lon": 2.0},
                ],
                request_key(query_gps): [{"off": True}, {"on": True}],
            },
            defaults={
                "card.location": {
                    "lat": 1.0,
                    "lon": 2.0,
                    "status": "fixed location",
                    "mode": "off",
                }
            },
        )
        source = inspect_location(
            ScriptedClient(
                defaults={
                    "card.location": {
                        "lat": 34.0,
                        "lon": -82.0,
                        "status": "GPS updated {gps}",
                    }
                }
            )
        )
        with self.assertRaisesRegex(SatphoneError, "requested coordinates"):
            set_fixed_test_location(client, source)


class ReportingTests(unittest.TestCase):
    def test_report_includes_raw_json_and_privacy_warning(self):
        snapshot = DiagnosticSnapshot(
            timestamp=datetime.now().astimezone(),
            serial_port="/dev/cu.test",
            checks=[DiagnosticCheck("Notecard", CheckLevel.PASS, "Connected")],
            raw={"card.version": {"version": "11"}},
        )
        report = format_report(snapshot)
        self.assertIn("RAW RESPONSES", report)
        self.assertIn('"version": "11"', report)
        self.assertIn("precise location", report)

        with tempfile.TemporaryDirectory() as directory:
            path = export_report(snapshot, logs_dir=Path(directory))
            self.assertTrue(path.exists())
            self.assertIn("SATPHONE DIAGNOSTIC REPORT", path.read_text())
            second = export_report(snapshot, logs_dir=Path(directory))
            self.assertNotEqual(path, second)
            self.assertTrue(second.exists())


if __name__ == "__main__":
    unittest.main()
