"""Exclusive raw USB trace capture; never used for application decisions."""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .notecard import NotecardClient, UnsafeSerialStateError


def _raw_command(serial_port: object, command: dict) -> None:
    payload = json.dumps(command, separators=(",", ":")).encode("utf-8") + b"\n"
    serial_port.write(payload)
    if hasattr(serial_port, "flush"):
        serial_port.flush()


def capture_trace(
    client: NotecardClient,
    duration_seconds: int = 120,
    sync_direction: Optional[str] = None,
    logs_dir: Path = Path("logs"),
    line_callback: Optional[Callable[[str], None]] = None,
) -> Path:
    """Capture undocumented trace text while normal JSON transactions are paused."""
    if sync_direction not in (None, "in", "out"):
        raise ValueError("sync_direction must be None, 'in', or 'out'")
    if duration_seconds < 1:
        raise ValueError("duration_seconds must be positive")

    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
    base_name = "{}-ntn-debug".format(stamp)
    path = (logs_dir / "{}.txt".format(base_name)).resolve()
    serial_port = client.serial_port
    previous_timeout = getattr(serial_port, "timeout", None)
    enable_attempted = False
    cleanup_errors = []
    primary_error = None

    interrupted = False
    try:
        # card.trace returns structured acknowledgement before raw text begins.
        # The command may reach the device even if its acknowledgement is lost,
        # so cleanup must attempt trace-off from this point onward.
        enable_attempted = True
        client.request({"req": "card.trace", "mode": "on"})
        serial_port.timeout = 0.25

        handle = None
        for index in range(1, 10000):
            suffix = "" if index == 1 else "-{}".format(index)
            path = (logs_dir / "{}{}.txt".format(base_name, suffix)).resolve()
            try:
                handle = path.open("x", encoding="utf-8")
                break
            except FileExistsError:
                continue
        if handle is None:
            raise OSError("Could not allocate a unique trace log filename")

        with handle:
            handle.write("SATPHONE RAW NOTECARD TRACE\n")
            handle.write("Started: {}\n".format(datetime.now().astimezone().isoformat()))
            handle.write("Serial port: {}\n".format(client.device))
            handle.write("Requested action: {}\n\n".format(sync_direction or "monitor only"))
            handle.flush()

            if sync_direction is not None:
                # Command form deliberately expects no JSON response, avoiding a
                # second reader while trace owns the serial stream.
                _raw_command(
                    serial_port,
                    {"cmd": "hub.sync", sync_direction: True},
                )

            deadline = time.monotonic() + duration_seconds
            while time.monotonic() < deadline:
                data = serial_port.readline()
                if not data:
                    continue
                text = data.decode("utf-8", errors="replace").rstrip("\r\n")
                rendered = "{} {}".format(
                    datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                    text,
                )
                handle.write(rendered + "\n")
                handle.flush()
                if line_callback is not None:
                    line_callback(rendered)
    except KeyboardInterrupt:
        interrupted = True
    except BaseException as exc:
        primary_error = exc
    finally:
        if enable_attempted:
            try:
                _raw_command(serial_port, {"cmd": "card.trace", "mode": "off"})
                time.sleep(0.5)
            except BaseException as exc:
                cleanup_errors.append("trace-off command failed: {}".format(exc))
        try:
            if hasattr(serial_port, "reset_input_buffer"):
                serial_port.reset_input_buffer()
        except BaseException as exc:
            cleanup_errors.append("input buffer cleanup failed: {}".format(exc))
        try:
            serial_port.timeout = previous_timeout
        except BaseException as exc:
            cleanup_errors.append("serial timeout restore failed: {}".format(exc))
        try:
            client.resynchronize_serial()
        except BaseException as exc:
            cleanup_errors.append("serial resynchronization failed: {}".format(exc))
    if cleanup_errors:
        try:
            client.close()
        except BaseException as exc:
            cleanup_errors.append("serial close failed: {}".format(exc))
        primary_detail = (
            " Capture also failed: {}.".format(primary_error)
            if primary_error is not None
            else ""
        )
        raise UnsafeSerialStateError(
            "Trace cleanup could not be confirmed ({}).{} The serial connection "
            "was closed; unplug/replug the Notecard before sending another request. "
            "Any created log is at {}.".format(
                "; ".join(cleanup_errors), primary_detail, path
            )
        ) from primary_error
    if primary_error is not None:
        raise primary_error
    if interrupted and line_callback is not None:
        line_callback("Trace stopped by user; trace-off cleanup completed.")
    return path
