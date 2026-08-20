"""Small, conservative Notehub maintenance client for inbound QI Notes."""

from __future__ import annotations

import http.client
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlencode

from . import INBOUND_FILE
from .discord_bridge import MAX_RESPONSE_BYTES, NOTEHUB_HOST


class NotehubAdminError(RuntimeError):
    """Notehub returned a definite error before completing the operation."""


class NotehubUncertainError(NotehubAdminError):
    """A non-idempotent request may have reached Notehub."""


@dataclass(frozen=True)
class NotehubNote:
    note_id: str
    body: Dict[str, Any]
    time: Optional[int] = None

    @property
    def text(self) -> str:
        message = self.body.get("msg")
        return str(message) if message is not None else json.dumps(self.body, sort_keys=True)


NotehubAdminTransport = Callable[
    [str, str, Optional[bytes], Dict[str, str], float], Tuple[int, bytes]
]


class NotehubAdminClient:
    """Preview and explicitly delete queued Notehub Notes without blind retry."""

    def __init__(
        self,
        project_uid: str,
        device_uid: str,
        token: str,
        notefile: str = INBOUND_FILE,
        timeout_seconds: float = 15.0,
        transport: Optional[NotehubAdminTransport] = None,
    ):
        if not str(project_uid).strip() or not str(device_uid).strip():
            raise ValueError("ProjectUID and DeviceUID are required.")
        if not str(token).strip():
            raise ValueError("A Notehub token is required.")
        if notefile != INBOUND_FILE:
            raise ValueError("Maintenance is limited to {}.".format(INBOUND_FILE))
        self.project_uid = str(project_uid).strip()
        self.device_uid = str(device_uid).strip()
        self.notefile = notefile
        self._token = str(token).strip()
        self.timeout_seconds = float(timeout_seconds)
        self._transport = transport or self._send

    @property
    def notefile_path(self) -> str:
        segments = (self.project_uid, self.device_uid, self.notefile)
        return "/v1/projects/{}/devices/{}/notes/{}".format(
            *(quote(value, safe="") for value in segments)
        )

    @staticmethod
    def _send(
        method: str,
        path: str,
        body: Optional[bytes],
        headers: Dict[str, str],
        timeout_seconds: float,
    ) -> Tuple[int, bytes]:
        connection = http.client.HTTPSConnection(NOTEHUB_HOST, timeout=timeout_seconds)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_RESPONSE_BYTES:
                raise NotehubAdminError("Notehub returned an unexpectedly large response.")
            return response.status, response_body
        finally:
            connection.close()

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": "Bearer {}".format(self._token),
            "Accept": "application/json",
            "User-Agent": "satphone-mac-app/1.0",
        }

    @staticmethod
    def _decode(response_body: bytes) -> Dict[str, Any]:
        if not response_body.strip():
            return {}
        try:
            decoded = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NotehubAdminError("Notehub returned malformed JSON.") from exc
        if not isinstance(decoded, dict):
            raise NotehubAdminError("Notehub returned an unexpected response shape.")
        return decoded

    def list_pending(self, maximum: int = 100) -> List[NotehubNote]:
        if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 500:
            raise ValueError("maximum must be from 1 through 500")
        path = "{}/changes?{}".format(
            self.notefile_path,
            urlencode({"max": maximum}),
        )
        try:
            status, body = self._transport(
                "GET", path, None, self._headers(), self.timeout_seconds
            )
        except NotehubAdminError:
            raise
        except Exception as exc:
            raise NotehubAdminError(
                "Could not read the Notehub queue: {}".format(exc)
            ) from exc
        decoded = self._decode(body)
        if not 200 <= status < 300:
            detail = decoded.get("err") or decoded.get("error") or "HTTP {}".format(status)
            raise NotehubAdminError("Notehub queue read failed: {}".format(detail))
        notes = decoded.get("notes")
        if notes in (None, {}):
            total = decoded.get("total", 0)
            if total in (None, 0):
                return []
            raise NotehubAdminError(
                "Notehub reported queued Notes but did not return their IDs."
            )
        if not isinstance(notes, dict):
            raise NotehubAdminError("Notehub returned an invalid Notes list.")
        result: List[NotehubNote] = []
        for note_id, value in notes.items():
            if not isinstance(note_id, str) or not note_id:
                raise NotehubAdminError("Notehub returned an invalid Note ID.")
            if not isinstance(value, dict) or not isinstance(value.get("body"), dict):
                raise NotehubAdminError(
                    "Notehub returned malformed content for Note {}.".format(note_id)
                )
            timestamp = value.get("time")
            if timestamp is not None and (
                not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp < 0
            ):
                raise NotehubAdminError(
                    "Notehub returned an invalid timestamp for Note {}.".format(note_id)
                )
            result.append(NotehubNote(note_id, dict(value["body"]), timestamp))
        result.sort(key=lambda item: (item.time or 0, item.note_id))
        return result

    def delete_note(self, note_id: str) -> None:
        """Delete one Note exactly once; transport ambiguity is surfaced."""
        note_id = str(note_id).strip()
        if not note_id or "/" in note_id:
            raise ValueError("A valid Note ID is required.")
        path = "{}/{}".format(self.notefile_path, quote(note_id, safe=""))
        try:
            status, body = self._transport(
                "DELETE", path, None, self._headers(), self.timeout_seconds
            )
        except NotehubAdminError:
            raise
        except Exception as exc:
            raise NotehubUncertainError(
                "The delete response was lost. Note {} may already be deleted; refresh before retrying.".format(
                    note_id
                )
            ) from exc
        decoded = self._decode(body)
        if not 200 <= status < 300:
            detail = decoded.get("err") or decoded.get("error") or "HTTP {}".format(status)
            raise NotehubAdminError(
                "Notehub rejected deletion of {}: {}".format(note_id, detail)
            )
        if decoded not in ({}, {"deleted": True}):
            raise NotehubUncertainError(
                "Notehub returned an unfamiliar success response for {}. Refresh before doing anything else.".format(
                    note_id
                )
            )

    def delete_notes(self, note_ids: Iterable[str]) -> List[str]:
        deleted: List[str] = []
        for note_id in note_ids:
            self.delete_note(note_id)
            deleted.append(str(note_id))
        return deleted
