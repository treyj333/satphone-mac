import unittest

from satphone.models import Message
from satphone.messages import (
    INBOUND_TEMPLATE,
    MessageDeletionError,
    inbox_count,
    OUTBOUND_TEMPLATE,
    message_size_bytes,
    queue_outbound_message,
    read_and_delete_messages,
    read_messages,
    repair_template,
    validate_message,
    verify_template,
)
from tests.fakes import ScriptedClient, request_key
from satphone.notecard import SatphoneError


class TemplateTests(unittest.TestCase):
    def test_valid_template_query_is_non_mutating(self):
        request = {"req": "note.template", "file": "messages.qi", "verify": True}
        client = ScriptedClient(
            scripted={
                request_key(request): [
                    {
                        "template": True,
                        "format": "compact",
                        "port": 56,
                        "body": {"msg": "-"},
                    }
                ]
            }
        )
        check = verify_template(client, INBOUND_TEMPLATE)
        self.assertTrue(check.valid)
        self.assertTrue(client.requests[0]["verify"])

    def test_missing_template_is_distinct(self):
        client = ScriptedClient(defaults={"note.template": {"template": False}})
        check = verify_template(client, INBOUND_TEMPLATE)
        self.assertFalse(check.exists)
        self.assertFalse(check.valid)

    def test_delete_semantics_are_compared(self):
        client = ScriptedClient(
            defaults={
                "note.template": {
                    "template": True,
                    "format": "compact",
                    "port": 56,
                    "body": {"msg": "-"},
                    "delete": True,
                }
            }
        )
        check = verify_template(client, INBOUND_TEMPLATE)
        self.assertFalse(check.valid)
        self.assertIn("delete", check.differences)

    def test_unknown_template_query_error_is_not_repairable(self):
        client = ScriptedClient(
            defaults={"note.template": {"err": "USB read failed {io}"}}
        )
        check = verify_template(client, INBOUND_TEMPLATE)
        self.assertFalse(check.valid)
        self.assertFalse(check.repairable)
        self.assertIn("USB read failed", check.query_error)

    def test_non_boolean_template_fields_are_not_trusted_or_repaired(self):
        for response in (
            {
                "template": "true",
                "format": "compact",
                "port": 56,
                "body": {"msg": "-"},
            },
            {
                "template": True,
                "format": "compact",
                "port": 56,
                "body": {"msg": "-"},
                "delete": "false",
            },
        ):
            with self.subTest(response=response):
                check = verify_template(
                    ScriptedClient(defaults={"note.template": response}),
                    INBOUND_TEMPLATE,
                )
                self.assertFalse(check.valid)
                self.assertFalse(check.repairable)
                self.assertTrue(check.query_error)

    def test_template_repair_aborts_if_state_changed_after_display(self):
        verify_request = {
            "req": "note.template",
            "file": "messages.qi",
            "verify": True,
        }
        displayed_response = {"template": False}
        client = ScriptedClient(
            scripted={
                request_key(verify_request): [
                    displayed_response,
                    {
                        "template": True,
                        "format": "compact",
                        "port": 99,
                        "body": {"msg": "-"},
                    },
                ]
            }
        )
        displayed = verify_template(client, INBOUND_TEMPLATE)
        with self.assertRaisesRegex(SatphoneError, "state changed"):
            repair_template(client, INBOUND_TEMPLATE, expected=displayed)
        writes = [
            request
            for request in client.requests
            if request.get("req") == "note.template" and not request.get("verify")
        ]
        self.assertEqual(writes, [])

    def test_template_repair_rechecks_and_verifies_the_write(self):
        verify_request = {
            "req": "note.template",
            "file": "messages.qi",
            "verify": True,
        }
        missing = {"template": False}
        valid = {
            "template": True,
            "format": "compact",
            "port": 56,
            "body": {"msg": "-"},
        }
        client = ScriptedClient(
            scripted={
                request_key(verify_request): [missing, missing, valid],
                request_key(INBOUND_TEMPLATE.request()): [{}],
            }
        )
        displayed = verify_template(client, INBOUND_TEMPLATE)
        repaired = repair_template(client, INBOUND_TEMPLATE, expected=displayed)
        self.assertTrue(repaired.valid)

    def test_template_repair_ignores_per_transaction_crc_changes(self):
        verify_request = {
            "req": "note.template",
            "file": "messages.qi",
            "verify": True,
        }
        missing_first = {"template": False, "crc": "0001:AAAA"}
        missing_second = {"template": False, "crc": "0002:BBBB"}
        valid = {
            "template": True,
            "format": "compact",
            "port": 56,
            "body": {"msg": "-"},
            "crc": "0004:DDDD",
        }
        client = ScriptedClient(
            scripted={
                request_key(verify_request): [missing_first, missing_second, valid],
                request_key(INBOUND_TEMPLATE.request()): [{"crc": "0003:CCCC"}],
            }
        )
        displayed = verify_template(client, INBOUND_TEMPLATE)
        repaired = repair_template(client, INBOUND_TEMPLATE, expected=displayed)
        self.assertTrue(repaired.valid)


class MessageTests(unittest.TestCase):
    def test_read_only_uses_note_changes_without_delete(self):
        client = ScriptedClient(
            defaults={
                "note.changes": {
                    "total": 2,
                    "notes": {
                        "b": {"body": {"msg": "second"}, "time": 20},
                        "a": {"body": {"msg": "first"}, "time": 10},
                    },
                }
            }
        )
        messages = read_messages(client)
        self.assertEqual([item.text for item in messages], ["first", "second"])
        self.assertNotIn("delete", client.requests[0])

    def test_canonical_missing_notefile_is_a_normal_empty_inbox(self):
        missing = {"err": "Notefile does not exist {notefile-noexist}"}
        self.assertEqual(inbox_count(ScriptedClient(defaults={"file.changes": missing})), 0)
        self.assertEqual(
            read_messages(ScriptedClient(defaults={"note.changes": missing})), []
        )

    def test_optional_empty_inbox_shapes_are_normal(self):
        self.assertEqual(
            inbox_count(ScriptedClient(defaults={"file.changes": {}})), 0
        )
        self.assertEqual(
            inbox_count(
                ScriptedClient(defaults={"file.changes": {"total": 0}})
            ),
            0,
        )
        self.assertEqual(
            read_messages(ScriptedClient(defaults={"note.changes": {}})), []
        )
        self.assertEqual(
            read_messages(
                ScriptedClient(defaults={"note.changes": {"total": 0}})
            ),
            [],
        )

    def test_positive_or_malformed_inbox_responses_fail_closed(self):
        with self.assertRaisesRegex(SatphoneError, "without per-file"):
            inbox_count(
                ScriptedClient(defaults={"file.changes": {"total": 1}})
            )
        with self.assertRaisesRegex(SatphoneError, "invalid inbox shape"):
            read_messages(
                ScriptedClient(defaults={"note.changes": {"notes": []}})
            )
        with self.assertRaisesRegex(SatphoneError, "no valid"):
            inbox_count(
                ScriptedClient(
                    defaults={
                        "file.changes": {
                            "total": 1,
                            "info": {"messages.qi": {"total": 1.5}},
                        }
                    }
                )
            )
        with self.assertRaisesRegex(SatphoneError, "invalid inbox shape"):
            read_messages(
                ScriptedClient(
                    defaults={
                        "note.changes": {
                            "total": 1.5,
                            "notes": {
                                "a": {"body": {"msg": "hello"}, "time": 1}
                            },
                        }
                    }
                )
            )
        with self.assertRaisesRegex(SatphoneError, "contains 1 Note"):
            read_messages(
                ScriptedClient(
                    defaults={
                        "note.changes": {"total": 1, "changes": 0, "notes": {}}
                    }
                )
            )

    def test_inbox_count_uses_the_selected_notefile_entry(self):
        client = ScriptedClient(
            defaults={
                "file.changes": {
                    "total": 7,
                    "info": {"messages.qi": {"changes": 2, "total": 2}},
                }
            }
        )
        self.assertEqual(inbox_count(client), 2)

    def test_invalid_inbox_timestamp_is_explicit(self):
        for timestamp in (None, "yesterday", True, 1.5, -1):
            with self.subTest(timestamp=timestamp):
                note = {"body": {"msg": "hello"}}
                if timestamp is not None:
                    note["time"] = timestamp
                client = ScriptedClient(
                    defaults={
                        "note.changes": {
                            "total": 1,
                            "notes": {"a": note},
                        }
                    }
                )
                with self.assertRaisesRegex(SatphoneError, "invalid timestamp"):
                    read_messages(client)

    def test_read_and_delete_only_uses_explicit_delete(self):
        client = ScriptedClient(
            scripted={
                request_key(
                    {"req": "note.get", "file": "messages.qi", "delete": True}
                ): [{"body": {"msg": "hello"}, "time": 10}],
            },
        )
        messages = read_and_delete_messages(client, maximum=1)
        self.assertEqual([item.text for item in messages], ["hello"])
        destructive = [item for item in client.requests if item.get("delete")]
        self.assertEqual(
            destructive,
            [{"req": "note.get", "file": "messages.qi", "delete": True}],
        )

    def test_final_transport_failure_is_uncertain_and_not_app_retried(self):
        class BrokenDeleteClient(ScriptedClient):
            def inspect(self, request):
                self.requests.append(dict(request))
                raise RuntimeError("transaction exhausted")

        client = BrokenDeleteClient()
        with self.assertRaises(MessageDeletionError) as raised:
            read_and_delete_messages(client, maximum=1)
        self.assertTrue(raised.exception.uncertain_latest)
        destructive = [item for item in client.requests if item.get("delete")]
        self.assertEqual(len(destructive), 1)
        self.assertEqual(destructive[0].get("req"), "note.get")

    def test_keyboard_interrupt_during_delete_is_reported_uncertain(self):
        class InterruptedDeleteClient(ScriptedClient):
            def inspect(self, request):
                self.requests.append(dict(request))
                raise KeyboardInterrupt

        client = InterruptedDeleteClient()
        with self.assertRaises(MessageDeletionError) as raised:
            read_and_delete_messages(client, maximum=1)
        self.assertEqual(raised.exception.uncertain_count, 1)
        self.assertEqual(len(client.requests), 1)

    def test_partial_deletion_count_is_preserved_on_later_error(self):
        class FailingRequestClient(ScriptedClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.delete_calls = 0

            def inspect(self, request):
                if request.get("req") == "note.get":
                    self.delete_calls += 1
                    if self.delete_calls == 2:
                        self.requests.append(dict(request))
                        raise RuntimeError("transaction exhausted")
                return super().inspect(request)

        client = FailingRequestClient(
            scripted={
                request_key(
                    {"req": "note.get", "file": "messages.qi", "delete": True}
                ): [{"body": {"msg": "deleted"}, "time": 10}],
            },
        )
        with self.assertRaises(MessageDeletionError) as raised:
            read_and_delete_messages(client, maximum=2)
        self.assertEqual([item.text for item in raised.exception.deleted], ["deleted"])
        self.assertTrue(raised.exception.uncertain_latest)
        self.assertEqual(raised.exception.uncertain_count, 1)

    def test_malformed_delete_response_marks_at_most_one_note_uncertain(self):
        client = ScriptedClient(
            scripted={
                request_key(
                    {"req": "note.get", "file": "messages.qi", "delete": True}
                ): [{}],
            }
        )
        with self.assertRaises(MessageDeletionError) as raised:
            read_and_delete_messages(client, maximum=1)
        self.assertEqual(raised.exception.uncertain_count, 1)
        requests = [item for item in client.requests if item.get("req") == "note.get"]
        self.assertEqual(len(requests), 1)

    def test_changed_displayed_set_aborts_before_destructive_command(self):
        displayed = [Message(body={"msg": "first"}, time=10, note_id="a")]
        client = ScriptedClient(
            defaults={
                "note.changes": {
                    "total": 1,
                    "notes": {"b": {"body": {"msg": "new"}, "time": 20}},
                }
            }
        )
        with self.assertRaisesRegex(MessageDeletionError, "inbox changed"):
            read_and_delete_messages(
                client, maximum=1, expected=displayed
            )
        self.assertFalse(any(item.get("delete") for item in client.requests))

    def test_same_second_notes_match_in_any_oldest_queue_order(self):
        displayed = [
            Message(body={"msg": "alpha"}, time=10, note_id="a"),
            Message(body={"msg": "beta"}, time=10, note_id="b"),
        ]
        delete_request = {
            "req": "note.get",
            "file": "messages.qi",
            "delete": True,
        }
        client = ScriptedClient(
            scripted={
                request_key(delete_request): [
                    {"body": {"msg": "beta"}, "time": 10},
                    {"body": {"msg": "alpha"}, "time": 10},
                ]
            },
            defaults={
                "note.changes": {
                    "total": 2,
                    "notes": {
                        "a": {"body": {"msg": "alpha"}, "time": 10},
                        "b": {"body": {"msg": "beta"}, "time": 10},
                    },
                }
            },
        )
        deleted = read_and_delete_messages(
            client, maximum=2, expected=displayed
        )
        self.assertEqual([message.text for message in deleted], ["beta", "alpha"])

    def test_queue_does_not_create_second_sync_path(self):
        client = ScriptedClient(defaults={"note.add": {"total": 1, "template": True}})
        queue_outbound_message(client, "hello", {"to": "mary"})
        request = next(item for item in client.requests if item.get("req") == "note.add")
        self.assertEqual(request["file"], OUTBOUND_TEMPLATE.file)
        self.assertEqual(request["body"], {"to": "mary", "msg": "hello"})
        self.assertNotIn("sync", request)

    def test_queue_requires_a_valid_structured_success_response(self):
        for response in ({}, {"total": True}, {"total": -1}):
            with self.subTest(response=response):
                client = ScriptedClient(defaults={"note.add": response})
                with self.assertRaisesRegex(SatphoneError, "invalid success"):
                    queue_outbound_message(client, "hello")
                self.assertEqual(len(client.requests), 1)

    def test_queue_transport_failure_is_wrapped_as_uncertain_once(self):
        class BrokenQueueClient:
            def __init__(self):
                self.calls = 0

            def request(self, request):
                self.calls += 1
                raise RuntimeError("Failed to transact")

        client = BrokenQueueClient()
        with self.assertRaisesRegex(SatphoneError, "outcome is uncertain"):
            queue_outbound_message(client, "hello")
        self.assertEqual(client.calls, 1)

    def test_keyboard_interrupt_during_queue_is_reported_uncertain(self):
        class InterruptedQueueClient:
            def __init__(self):
                self.calls = 0

            def request(self, request):
                self.calls += 1
                raise KeyboardInterrupt

        client = InterruptedQueueClient()
        with self.assertRaisesRegex(SatphoneError, "outcome is uncertain"):
            queue_outbound_message(client, "hello")
        self.assertEqual(client.calls, 1)

    def test_utf8_limit_counts_bytes(self):
        self.assertEqual(message_size_bytes("é"), 2)
        with self.assertRaises(ValueError):
            validate_message("é" * 81)


if __name__ == "__main__":
    unittest.main()
