"""USB serial discovery and the sole note-python transaction boundary."""

import copy
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


BLUES_USB_VID = 0x30A4
NOTECARD_USB_PID = 0x0001


class SatphoneError(Exception):
    """Base application error."""


class DeviceNotFoundError(SatphoneError):
    """Raised when no plausible Notecard serial port is visible."""


class UnsafeSerialStateError(SatphoneError):
    """Raised after raw trace cleanup cannot restore structured transactions."""


class NotecardRequestError(SatphoneError):
    """A structured error returned by the Notecard."""

    def __init__(self, request: Dict[str, Any], response: Dict[str, Any]):
        self.request = request
        self.response = response
        super().__init__(str(response.get("err", "unknown Notecard error")))


@dataclass(frozen=True)
class SerialDevice:
    device: str
    description: str
    manufacturer: str
    vid: Optional[int]
    pid: Optional[int]


@dataclass
class TransactionRecord:
    timestamp: str
    request: Dict[str, Any]
    response: Optional[Dict[str, Any]]
    error: str
    duration_ms: int

    def as_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "request": copy.deepcopy(self.request),
            "response": copy.deepcopy(self.response),
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


def _serial_device(port: Any) -> SerialDevice:
    return SerialDevice(
        device=str(port.device),
        description=str(port.description or "Unknown serial device"),
        manufacturer=str(port.manufacturer or ""),
        vid=getattr(port, "vid", None),
        pid=getattr(port, "pid", None),
    )


def list_serial_devices() -> List[SerialDevice]:
    """Return all serial devices without opening any of them."""
    try:
        from serial.tools import list_ports
    except ImportError as exc:
        raise SatphoneError(
            "PySerial is not installed. Run: python3 -m pip install -r requirements.txt"
        ) from exc
    return [_serial_device(port) for port in list_ports.comports()]


def discover_notecard_ports(devices: Optional[List[SerialDevice]] = None) -> List[SerialDevice]:
    """Find likely Notecard ports using Blues USB IDs, then safe name fallbacks."""
    devices = list(devices) if devices is not None else list_serial_devices()

    exact = [
        item
        for item in devices
        if item.vid == BLUES_USB_VID and item.pid == NOTECARD_USB_PID
    ]
    blues = [item for item in devices if item.vid == BLUES_USB_VID]
    named = []
    for item in devices:
        haystack = "{} {} {}".format(
            item.device, item.description, item.manufacturer
        ).lower()
        if (
            "usbmodemnote" in haystack
            or "notecard" in haystack
            or "blues wireless" in haystack
            or "blues inc" in haystack
        ):
            named.append(item)

    matches = exact or blues or named
    if not matches:
        matches = [
            item
            for item in devices
            if "usbmodem" in item.device.lower()
        ]

    # macOS exposes tty/cu twins. Prefer the call-out node and de-duplicate it.
    cu_matches = [item for item in matches if item.device.startswith("/dev/cu.")]
    selected = cu_matches or matches
    seen = set()
    unique = []
    for item in selected:
        if item.device not in seen:
            unique.append(item)
            seen.add(item.device)
    return sorted(unique, key=lambda item: item.device)


def has_error_tag(value: Any, tag: str) -> bool:
    return "{{{}}}".format(tag).lower() in str(value or "").lower()


def notecard_identity_error(response: Dict[str, Any]) -> Optional[str]:
    """Validate the documented minimum identity returned by card.version."""
    if response.get("err"):
        return str(response.get("err"))
    version = response.get("version")
    if not isinstance(version, str) or not version.strip():
        return "card.version did not return a firmware version"
    return None


class NotecardClient:
    """Records every structured request while isolating hardware dependencies."""

    def __init__(self, card: Any, serial_port: Any, device: str):
        self.card = card
        self.serial_port = serial_port
        self.device = device
        self._history: List[TransactionRecord] = []

    @classmethod
    def open(cls, device: str) -> "NotecardClient":
        try:
            import notecard as note_python
            import serial
        except ImportError as exc:
            raise SatphoneError(
                "Notecard dependencies are missing. Run: python3 -m pip install -r requirements.txt"
            ) from exc

        port = None
        try:
            port = serial.Serial(
                port=device,
                baudrate=9600,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=10,
                write_timeout=10,
            )
            card = note_python.OpenSerial(port, debug=False)
            if hasattr(card, "SetAppUserAgent"):
                card.SetAppUserAgent(
                    {"app": "satphone-development-tool", "app_version": "1.0.0"}
                )
            client = cls(card=card, serial_port=port, device=device)
            version = client.request({"req": "card.version"})
            identity_error = notecard_identity_error(version)
            if identity_error:
                raise SatphoneError(identity_error)
            return client
        except Exception as exc:
            if port is not None:
                try:
                    port.close()
                except Exception:
                    pass
            raise SatphoneError(
                "Could not open the Notecard on {}: {}. Disconnect the Blues browser terminal and try again.".format(
                    device, exc
                )
            ) from exc

    @property
    def history(self) -> List[Dict[str, Any]]:
        return [record.as_dict() for record in self._history]

    def request(
        self, request: Dict[str, Any], raise_on_error: bool = True
    ) -> Dict[str, Any]:
        started = time.monotonic()
        response = None
        error = ""
        try:
            response = self.card.Transaction(copy.deepcopy(request))
            if response is None:
                raise SatphoneError(
                    "Notecard returned no structured response for {}".format(
                        request.get("req", request.get("cmd", "request"))
                    )
                )
            if not isinstance(response, dict):
                raise SatphoneError(
                    "Notecard returned a non-object response for {}".format(
                        request.get("req", request.get("cmd", "request"))
                    )
                )
            if response.get("err") and raise_on_error:
                raise NotecardRequestError(request, response)
            return response
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            elapsed = int((time.monotonic() - started) * 1000)
            self._history.append(
                TransactionRecord(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    request=copy.deepcopy(request),
                    response=copy.deepcopy(response),
                    error=error,
                    duration_ms=elapsed,
                )
            )

    def inspect(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Return structured error responses for diagnosis instead of raising."""
        return self.request(request, raise_on_error=False)

    def resynchronize_serial(self) -> None:
        """Re-align note-python after exclusive raw trace mode."""
        self.card.Reset()

    def close(self) -> None:
        try:
            self.serial_port.close()
        except Exception:
            pass

    def __enter__(self) -> "NotecardClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
