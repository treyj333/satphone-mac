"""Shared, hardware-independent data models."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class CheckLevel(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    INFO = "info"


@dataclass
class DiagnosticCheck:
    name: str
    level: CheckLevel
    summary: str
    detail: str = ""
    recommendation: str = ""


@dataclass
class DiagnosticSnapshot:
    timestamp: datetime
    serial_port: str
    checks: List[DiagnosticCheck] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TemplateSpec:
    file: str
    port: int
    body: Dict[str, Any]
    format: str = "compact"

    def request(self) -> Dict[str, Any]:
        return {
            "req": "note.template",
            "file": self.file,
            "format": self.format,
            "port": self.port,
            "body": dict(self.body),
        }


@dataclass
class TemplateCheck:
    spec: TemplateSpec
    exists: bool
    valid: bool
    response: Dict[str, Any]
    differences: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    query_error: str = ""
    repairable: bool = False


@dataclass
class Message:
    body: Dict[str, Any]
    time: Optional[int] = None
    note_id: Optional[str] = None

    @property
    def text(self) -> str:
        value = self.body.get("msg")
        if value is not None:
            return str(value)
        return str(self.body)


@dataclass
class LocationInfo:
    known: bool
    latitude: Optional[float]
    longitude: Optional[float]
    captured_at: Optional[int]
    mode: Optional[str]
    status: str
    source: str
    dop: Optional[float]
    failure_count: Optional[int]
    raw: Dict[str, Any]


class SyncPhase(str, Enum):
    IDLE = "idle"
    REQUESTED = "requested"
    WAITING = "waiting"
    CONNECTING = "connecting"
    SYNCHRONIZING = "synchronizing"
    COMPLETED = "completed"
    FAILED = "failed"
    UNRESOLVED = "unresolved"
    TIMED_OUT = "timed_out"


@dataclass
class SyncUpdate:
    elapsed: int
    phase: SyncPhase
    message: str
    response: Dict[str, Any]


@dataclass
class SyncResult:
    direction: str
    phase: SyncPhase
    requested: bool
    updates: List[SyncUpdate]
    response: Dict[str, Any]
    error: str = ""

    @property
    def completed(self) -> bool:
        return self.phase == SyncPhase.COMPLETED
