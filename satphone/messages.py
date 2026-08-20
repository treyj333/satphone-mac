"""Templated inbound and outbound message operations."""

from typing import Any, Dict, List, Optional

from . import (
    INBOUND_FILE,
    INBOUND_PORT,
    MAX_MESSAGE_BYTES,
    MESSAGE_TEMPLATE_BODY,
    OUTBOUND_FILE,
    OUTBOUND_PORT,
)
from .models import Message, TemplateCheck, TemplateSpec
from .notecard import NotecardClient, NotecardRequestError, SatphoneError, has_error_tag


INBOUND_TEMPLATE = TemplateSpec(
    file=INBOUND_FILE,
    port=INBOUND_PORT,
    body=MESSAGE_TEMPLATE_BODY,
)
OUTBOUND_TEMPLATE = TemplateSpec(
    file=OUTBOUND_FILE,
    port=OUTBOUND_PORT,
    body=MESSAGE_TEMPLATE_BODY,
)


class MessageDeletionError(SatphoneError):
    """A destructive inbox read stopped after zero or more confirmed deletes."""

    def __init__(
        self,
        message: str,
        deleted: List[Message],
        response: Dict[str, Any],
        uncertain_latest: bool = False,
        uncertain_count: int = 0,
    ):
        self.deleted = list(deleted)
        self.response = response
        self.uncertain_count = max(0, int(uncertain_count))
        self.uncertain_latest = uncertain_latest or self.uncertain_count > 0
        super().__init__(message)


def _notefile_missing(value: Any) -> bool:
    # notefile-noexist is the current canonical status code. Retain the older
    # spelling as a compatibility fallback for deployed firmware.
    return has_error_tag(value, "notefile-noexist") or has_error_tag(
        value, "file-noexist"
    )


def _count_value(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def message_size_bytes(message: str) -> int:
    return len(message.encode("utf-8"))


def validate_message(message: str) -> str:
    cleaned = message.strip()
    if not cleaned:
        raise ValueError("Message cannot be empty.")
    size = message_size_bytes(cleaned)
    if size > MAX_MESSAGE_BYTES:
        raise ValueError(
            "Message is {} UTF-8 bytes; the development limit is {} bytes.".format(
                size, MAX_MESSAGE_BYTES
            )
        )
    return cleaned


def verify_template(client: NotecardClient, spec: TemplateSpec) -> TemplateCheck:
    # Omitting verify on a body-less note.template request can clear a template.
    response = client.inspect(
        {"req": "note.template", "file": spec.file, "verify": True}
    )
    error = str(response.get("err", ""))
    missing_error = has_error_tag(error, "note-noexist") or _notefile_missing(error)
    if error and not missing_error:
        return TemplateCheck(
            spec=spec,
            exists=False,
            valid=False,
            response=response,
            query_error=error,
            repairable=False,
        )

    template_value = response.get("template")
    if "template" in response and not isinstance(template_value, bool):
        return TemplateCheck(
            spec=spec,
            exists=False,
            valid=False,
            response=response,
            query_error="note.template returned a non-boolean template field",
            repairable=False,
        )
    if "delete" in response and not isinstance(response.get("delete"), bool):
        return TemplateCheck(
            spec=spec,
            exists=template_value is True,
            valid=False,
            response=response,
            query_error="note.template returned a non-boolean delete field",
            repairable=False,
        )

    exists = template_value is True and not missing_error

    differences: Dict[str, Dict[str, Any]] = {}
    if exists:
        expected = {
            "body": spec.body,
            "format": spec.format,
            "port": spec.port,
        }
        for field, expected_value in expected.items():
            actual_value = response.get(field)
            if actual_value != expected_value:
                differences[field] = {
                    "expected": expected_value,
                    "actual": actual_value,
                }
        # delete=true changes cross-transport cleanup semantics and is not part
        # of this message schema, so surface it as a meaningful mismatch.
        if response.get("delete") is True:
            differences["delete"] = {"expected": False, "actual": True}

    return TemplateCheck(
        spec=spec,
        exists=exists,
        valid=exists and not differences,
        response=response,
        differences=differences,
        query_error=error,
        repairable=(not exists or bool(differences)),
    )


def repair_template(
    client: NotecardClient,
    spec: TemplateSpec,
    expected: Optional[TemplateCheck] = None,
) -> TemplateCheck:
    """Re-read, apply an approved template change, then verify it."""
    current = verify_template(client, spec)
    if current.valid:
        return current
    if not current.repairable:
        raise SatphoneError(
            "Template state could not be verified immediately before repair; "
            "nothing was changed."
        )
    if expected is not None and (
        current.exists != expected.exists
        or current.valid != expected.valid
        # `crc` is a transaction checksum added by current Notecard firmware.
        # It changes on every otherwise-identical verify request and is not
        # template state, so it must not trip the concurrent-change guard.
        or {k: v for k, v in current.response.items() if k != "crc"}
        != {k: v for k, v in expected.response.items() if k != "crc"}
        or current.differences != expected.differences
    ):
        raise SatphoneError(
            "Template state changed after it was displayed; nothing was "
            "overwritten. Review the new state before approving a repair."
        )
    try:
        client.request(spec.request())
    except NotecardRequestError:
        raise
    except SatphoneError:
        raise
    except (Exception, KeyboardInterrupt) as exc:
        raise SatphoneError(
            "The template write did not return a final response and may have "
            "applied. Re-read the template before considering another write: "
            "{}".format(exc)
        ) from exc
    try:
        return verify_template(client, spec)
    except (Exception, KeyboardInterrupt) as exc:
        raise SatphoneError(
            "The template write returned, but verification was interrupted or "
            "failed. Re-read the template before retrying: {}".format(exc)
        ) from exc


def inbox_count(client: NotecardClient, file: str = INBOUND_FILE) -> int:
    response = client.inspect({"req": "file.changes", "files": [file]})
    if response.get("err"):
        if _notefile_missing(response.get("err")):
            return 0
        raise NotecardRequestError(
            {"req": "file.changes", "files": [file]}, response
        )
    info = response.get("info")
    top_total = response.get("total")
    if top_total is not None and not _count_value(top_total):
        raise SatphoneError(
            "file.changes returned an invalid total: {}".format(response)
        )
    if info is None:
        if top_total in (None, 0):
            return 0
        raise SatphoneError(
            "file.changes reported {} selected Note(s) without per-file "
            "details: {}".format(int(top_total), response)
        )
    if not isinstance(info, dict):
        raise SatphoneError(
            "file.changes returned invalid per-file details: {}".format(response)
        )
    file_info = info.get(file) if isinstance(info, dict) else None
    if file_info is None:
        if top_total in (None, 0):
            return 0
        raise SatphoneError(
            "file.changes reported {} selected Note(s) but omitted info[{}]: "
            "{}".format(int(top_total), file, response)
        )
    if not isinstance(file_info, dict):
        raise SatphoneError(
            "file.changes returned invalid info[{}]: {}".format(file, response)
        )
    total = file_info.get("total") if isinstance(file_info, dict) else None
    if total is None and top_total in (None, 0):
        return 0
    if not _count_value(total):
        raise SatphoneError(
            "file.changes returned no valid info[{}].total: {}".format(
                file, response
            )
        )
    return int(total)


def read_messages(client: NotecardClient, file: str = INBOUND_FILE) -> List[Message]:
    """List local inbound Notes without acknowledging or deleting them."""
    request = {"req": "note.changes", "file": file}
    response = client.inspect(request)
    if response.get("err"):
        if has_error_tag(response.get("err"), "note-noexist") or _notefile_missing(
            response.get("err")
        ):
            return []
        raise NotecardRequestError(request, response)

    total = response.get("total")
    notes = response.get("notes")
    if total is not None and not _count_value(total):
        raise SatphoneError(
            "note.changes returned an invalid inbox shape: {}".format(response)
        )
    if notes is not None and not isinstance(notes, dict):
        raise SatphoneError(
            "note.changes returned an invalid inbox shape: {}".format(response)
        )
    if not notes:
        if total in (None, 0):
            return []
        raise SatphoneError(
            "messages.qi contains {} Note(s), but note.changes returned none. "
            "No messages were deleted; inspect the change-tracker state.".format(
                int(total)
            )
        )
    if total == 0:
        raise SatphoneError(
            "note.changes returned Notes with total=0: {}".format(response)
        )
    messages = []
    for note_id, note in notes.items():
        if not isinstance(note, dict) or not isinstance(note.get("body"), dict):
            raise SatphoneError(
                "note.changes returned a malformed Note {}: {}".format(
                    note_id, note
                )
            )
        timestamp = note.get("time")
        if (
            not isinstance(timestamp, int)
            or isinstance(timestamp, bool)
            or timestamp < 0
        ):
            raise SatphoneError(
                "note.changes returned an invalid timestamp for Note {}: {}".format(
                    note_id, timestamp
                )
            )
        messages.append(
            Message(
                body=dict(note["body"]),
                time=timestamp,
                note_id=str(note_id),
            )
        )
    messages.sort(key=lambda item: (item.time or 0, item.note_id or ""))
    return messages


def read_and_delete_messages(
    client: NotecardClient,
    file: str = INBOUND_FILE,
    maximum: int = 100,
    expected: Optional[List[Message]] = None,
) -> List[Message]:
    """Pop local Notes only after the caller confirms the displayed queue prefix."""
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 0:
        raise ValueError("maximum must be a non-negative integer")
    targets: Optional[List[Message]] = None
    if expected is not None:
        expected = list(expected)
        if len(expected) > maximum:
            raise ValueError("expected messages exceed maximum")
        current = read_messages(client, file=file)
        if current[: len(expected)] != expected:
            raise MessageDeletionError(
                "The inbox changed after it was displayed, so no deletion command was sent.",
                deleted=[],
                response={"expected": len(expected), "current": len(current)},
            )
        targets = expected
    limit = len(targets) if targets is not None else maximum
    remaining_targets = list(targets) if targets is not None else None
    messages: List[Message] = []
    for _ in range(limit):
        request = {"req": "note.get", "file": file, "delete": True}
        try:
            response = client.inspect(request)
        except (Exception, KeyboardInterrupt) as exc:
            raise MessageDeletionError(
                "Inbox deletion stopped after {} confirmed message(s); the "
                "latest request outcome is uncertain: {}".format(
                    len(messages), exc
                ),
                deleted=messages,
                response={"_exception": str(exc)},
                uncertain_latest=True,
                uncertain_count=1,
            ) from exc
        if response.get("err"):
            if has_error_tag(
                response.get("err"), "note-noexist"
            ) or _notefile_missing(response.get("err")):
                if remaining_targets:
                    raise MessageDeletionError(
                        "The confirmed inbox changed before all displayed Notes "
                        "could be deleted; no further delete was attempted.",
                        deleted=messages,
                        response=response,
                    )
                break
            raise MessageDeletionError(
                "Inbox deletion stopped after {} confirmed message(s): {}".format(
                    len(messages), response.get("err")
                ),
                deleted=messages,
                response=response,
            )
        if "body" not in response or not isinstance(response.get("body"), dict):
            # A replay-safe Transaction may still have executed even if the
            # final response is malformed. Never add an application-level retry.
            raise MessageDeletionError(
                "Inbox deletion stopped after an unexpected note.get response; "
                "the latest Note's deletion state is uncertain.",
                deleted=messages,
                response=response,
                uncertain_latest=True,
                uncertain_count=1,
            )
        timestamp = response.get("time")
        timestamp_valid = (
            isinstance(timestamp, int)
            and not isinstance(timestamp, bool)
            and timestamp >= 0
        )
        message = Message(
            body=dict(response["body"]),
            time=timestamp if timestamp_valid else None,
        )
        messages.append(message)
        if not timestamp_valid:
            raise MessageDeletionError(
                "A Note was deleted, but note.get returned an invalid timestamp; "
                "no further delete was attempted.",
                deleted=messages,
                response=response,
            )
        if remaining_targets is not None:
            match_index = next(
                (
                    target_index
                    for target_index, target in enumerate(remaining_targets)
                    if message.body == target.body and message.time == target.time
                ),
                None,
            )
            if match_index is None:
                raise MessageDeletionError(
                    "The queue changed during deletion and an unexpected Note "
                    "was removed; no further delete was attempted.",
                    deleted=messages,
                    response=response,
                )
            remaining_targets.pop(match_index)
    return messages


def queue_outbound_message(
    client: NotecardClient,
    message: str,
    extra_fields: Dict[str, Any] = None,
) -> Dict[str, Any]:
    text = validate_message(message)
    body = dict(extra_fields or {})
    body["msg"] = text
    request = {"req": "note.add", "file": OUTBOUND_FILE, "body": body}
    try:
        response = client.request(request)
    except NotecardRequestError:
        raise
    except SatphoneError:
        raise
    except (Exception, KeyboardInterrupt) as exc:
        raise SatphoneError(
            "note.add exhausted the library transaction handling; the queue "
            "outcome is uncertain and no outer retry or sync should be sent: "
            "{}".format(exc)
        ) from exc
    total = response.get("total")
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or total < 0
        or (
            "template" in response
            and not isinstance(response.get("template"), bool)
        )
    ):
        raise SatphoneError(
            "note.add returned an invalid success response; queue state is "
            "uncertain and no sync should be requested: {}".format(response)
        )
    return response
