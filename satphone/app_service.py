"""Application-facing services that serialize Notecard USB operations."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .app_paths import app_support_dir
from .diagnostics import DiagnosticRunner
from .messages import (
    INBOUND_TEMPLATE,
    OUTBOUND_TEMPLATE,
    queue_outbound_message,
    read_and_delete_messages,
    read_messages,
    repair_template,
    verify_template,
)
from .models import DiagnosticSnapshot, Message, SyncResult, SyncUpdate, TemplateCheck
from .notecard import DeviceNotFoundError, NotecardClient, SerialDevice, discover_notecard_ports
from .reporting import export_report
from .satellite import SyncMonitor, ntn_status, set_ntn_transport, transport_status


@dataclass
class DiagnosticResult:
    snapshot: DiagnosticSnapshot
    templates: Dict[str, TemplateCheck]
    transaction_history: List[Dict[str, Any]]


@dataclass
class MessageSyncResult:
    sync: SyncResult
    messages: List[Message]


@dataclass
class SendResult:
    queued: Dict[str, Any]
    sync: SyncResult


class DeviceService:
    """Open the Notecard only for one serialized operation at a time."""

    def __init__(self, poll_seconds: float = 10.0, timeout_seconds: float = 900.0):
        self.poll_seconds = float(poll_seconds)
        self.timeout_seconds = float(timeout_seconds)
        self._usb_lock = threading.RLock()

    @staticmethod
    def ports() -> List[SerialDevice]:
        return discover_notecard_ports()

    @staticmethod
    def choose_port(port: Optional[str] = None) -> str:
        if port:
            return str(port)
        matches = discover_notecard_ports()
        if not matches:
            raise DeviceNotFoundError(
                "No Blues Notecard was found. Plug it in over USB, close any browser terminal, and refresh."
            )
        return matches[0].device

    def _with_client(self, port: Optional[str], action: Callable[[NotecardClient], Any]) -> Any:
        with self._usb_lock:
            selected = self.choose_port(port)
            with NotecardClient.open(selected) as client:
                return action(client)

    def diagnostics(self, port: Optional[str]) -> DiagnosticResult:
        def action(client: NotecardClient) -> DiagnosticResult:
            snapshot, templates = DiagnosticRunner(client).run()
            return DiagnosticResult(snapshot, templates, client.history)

        return self._with_client(port, action)

    def export_diagnostics(self, result: DiagnosticResult) -> Path:
        return export_report(
            result.snapshot,
            transaction_history=result.transaction_history,
            logs_dir=app_support_dir() / "reports",
        )

    def read_local_messages(self, port: Optional[str]) -> List[Message]:
        return self._with_client(port, read_messages)

    def receive_messages(
        self,
        port: Optional[str],
        callback: Optional[Callable[[SyncUpdate], None]] = None,
    ) -> MessageSyncResult:
        def action(client: NotecardClient) -> MessageSyncResult:
            sync = SyncMonitor(
                client,
                poll_seconds=self.poll_seconds,
                timeout_seconds=self.timeout_seconds,
            ).run("in", callback=callback)
            messages = read_messages(client) if sync.completed else []
            return MessageSyncResult(sync, messages)

        return self._with_client(port, action)

    def send_message(
        self,
        port: Optional[str],
        message: str,
        callback: Optional[Callable[[SyncUpdate], None]] = None,
    ) -> SendResult:
        def action(client: NotecardClient) -> SendResult:
            template = verify_template(client, OUTBOUND_TEMPLATE)
            if not template.valid:
                raise RuntimeError(
                    "The outbound template is not ready. Run Diagnostics and approve the template repair first."
                )
            queued = queue_outbound_message(client, message)
            sync = SyncMonitor(
                client,
                poll_seconds=self.poll_seconds,
                timeout_seconds=self.timeout_seconds,
            ).run("out", callback=callback)
            return SendResult(queued, sync)

        return self._with_client(port, action)

    def delete_local_messages(
        self, port: Optional[str], expected: List[Message]
    ) -> List[Message]:
        return self._with_client(
            port,
            lambda client: read_and_delete_messages(
                client, expected=list(expected), maximum=len(expected)
            ),
        )

    def repair_templates(
        self,
        port: Optional[str],
        expected: Optional[Dict[str, TemplateCheck]] = None,
    ) -> Dict[str, TemplateCheck]:
        def action(client: NotecardClient) -> Dict[str, TemplateCheck]:
            results: Dict[str, TemplateCheck] = {}
            for spec in (INBOUND_TEMPLATE, OUTBOUND_TEMPLATE):
                check = verify_template(client, spec)
                if check.valid:
                    results[spec.file] = check
                    continue
                approved_state = expected.get(spec.file) if expected else check
                results[spec.file] = repair_template(client, spec, expected=approved_state)
            return results

        return self._with_client(port, action)

    def get_transport(self, port: Optional[str]) -> Dict[str, Any]:
        return self._with_client(port, transport_status)

    def connection_status(self, port: Optional[str]) -> Dict[str, Dict[str, Any]]:
        """Read USB-visible satellite state without starting a paid sync."""
        return self._with_client(
            port,
            lambda client: {
                "ntn": ntn_status(client),
                "transport": transport_status(client),
            },
        )

    def repair_transport(self, port: Optional[str]) -> Dict[str, Dict[str, Any]]:
        return self._with_client(port, set_ntn_transport)
