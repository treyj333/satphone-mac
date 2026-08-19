import json
import unittest

from satphone.notehub_admin import (
    NotehubAdminClient,
    NotehubAdminError,
    NotehubUncertainError,
)


class RecordingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, path, body, headers, timeout):
        self.calls.append((method, path, body, headers, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class NotehubAdminTests(unittest.TestCase):
    def client(self, transport):
        return NotehubAdminClient("app:one", "dev:two", "secret-token", transport=transport)

    def test_lists_pending_notes_without_leaking_token_into_path(self):
        body = json.dumps(
            {
                "total": 2,
                "notes": {
                    "note-b": {"body": {"msg": "later"}, "time": 20},
                    "note-a": {"body": {"msg": "first"}, "time": 10},
                },
            }
        ).encode()
        transport = RecordingTransport([(200, body)])
        notes = self.client(transport).list_pending()
        self.assertEqual([note.note_id for note in notes], ["note-a", "note-b"])
        method, path, _, headers, _ = transport.calls[0]
        self.assertEqual(method, "GET")
        self.assertIn("/changes?max=100", path)
        self.assertNotIn("secret-token", path)
        self.assertEqual(headers["Authorization"], "Bearer secret-token")

    def test_empty_queue_is_normal(self):
        transport = RecordingTransport([(200, b'{"total":0}')])
        self.assertEqual(self.client(transport).list_pending(), [])

    def test_delete_is_sent_once_and_unknown_transport_result_is_uncertain(self):
        transport = RecordingTransport([TimeoutError("lost")])
        with self.assertRaises(NotehubUncertainError):
            self.client(transport).delete_note("note-1")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0][0], "DELETE")

    def test_definite_delete_rejection_is_not_called_success(self):
        transport = RecordingTransport([(403, b'{"err":"forbidden"}')])
        with self.assertRaises(NotehubAdminError):
            self.client(transport).delete_note("note-1")


if __name__ == "__main__":
    unittest.main()
