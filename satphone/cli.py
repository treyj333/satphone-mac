"""Minimal interactive macOS interface for the SATPHONE development tool."""

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from . import MAX_MESSAGE_BYTES
from .debug import capture_trace
from .diagnostics import DiagnosticRunner
from .location import (
    apply_location_configuration,
    inspect_location,
    location_configuration,
    location_configuration_error,
    set_fixed_test_location,
)
from .messages import (
    INBOUND_TEMPLATE,
    MessageDeletionError,
    OUTBOUND_TEMPLATE,
    queue_outbound_message,
    read_and_delete_messages,
    read_messages,
    repair_template,
    verify_template,
)
from .models import (
    CheckLevel,
    DiagnosticCheck,
    DiagnosticSnapshot,
    Message,
    SyncPhase,
    SyncResult,
    SyncUpdate,
    TemplateCheck,
)
from .notecard import (
    DeviceNotFoundError,
    NotecardClient,
    NotecardRequestError,
    SatphoneError,
    UnsafeSerialStateError,
    discover_notecard_ports,
    list_serial_devices,
)
from .reporting import export_report
from .satellite import (
    SyncMonitor,
    hub_sync_preflight_error,
    ntn_status,
    set_ntn_transport,
    status_tags,
    sync_status_query_error,
    sync_status_is_active,
    transport_status,
    transport_status_error,
    transport_supports_ntn,
)


SYMBOLS = {
    CheckLevel.PASS: "✓",
    CheckLevel.FAIL: "✗",
    CheckLevel.WARN: "!",
    CheckLevel.INFO: "•",
}


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def confirm(prompt: str, default: bool = False) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    answer = input(prompt + suffix).strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _choose_port(explicit_port: Optional[str]) -> str:
    if explicit_port:
        return explicit_port
    all_devices = list_serial_devices()
    candidates = discover_notecard_ports(all_devices)
    if not candidates:
        if all_devices:
            visible = "\n".join(
                "  {} ({})".format(item.device, item.description)
                for item in all_devices
            )
            raise DeviceNotFoundError(
                "No likely Notecard was found. Visible serial devices:\n{}".format(
                    visible
                )
            )
        raise DeviceNotFoundError("No serial devices are visible.")
    if len(candidates) == 1:
        return candidates[0].device

    print("\nMultiple possible Notecards found:")
    for index, item in enumerate(candidates, 1):
        print("  {}. {} ({})".format(index, item.device, item.description))
    while True:
        answer = input("Choose device number: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1].device
        print("Enter a number from 1 to {}.".format(len(candidates)))


def _print_check(check: DiagnosticCheck) -> None:
    print("{} {}: {}".format(SYMBOLS[check.level], check.name, check.summary))
    if check.detail:
        print("    {}".format(check.detail))
    if check.recommendation:
        print("    Recommended: {}".format(check.recommendation))


def print_diagnostic(snapshot: DiagnosticSnapshot) -> None:
    print("\nSATPHONE DIAGNOSTIC\n")
    for check in snapshot.checks:
        _print_check(check)

    problems = [
        check
        for check in snapshot.checks
        if check.level in (CheckLevel.FAIL, CheckLevel.WARN)
    ]
    if problems:
        print("\nMost important next actions:")
        for check in problems:
            if check.recommendation:
                print("  - {}: {}".format(check.name, check.recommendation))
    else:
        print("\nNo configuration problem was found by the read-only checks.")


def _describe_template(check: TemplateCheck) -> None:
    print("\n{} expected template:".format(check.spec.file))
    print(_json(check.spec.request()))
    if check.exists:
        print("Difference:")
        print(_json(check.differences))
    else:
        print("Current result:")
        print(_json(check.response))


def _offer_template_repairs(
    client: NotecardClient, checks: Dict[str, TemplateCheck]
) -> None:
    for check in checks.values():
        if check.valid:
            continue
        _describe_template(check)
        if not check.repairable:
            print(
                "Template state is unknown because verification failed; automatic repair is disabled."
            )
            continue
        if not confirm(
            "Repair {}? This is a persistent template change.".format(
                check.spec.file
            )
        ):
            print("Template left unchanged.")
            continue
        try:
            repaired = repair_template(client, check.spec, expected=check)
        except SatphoneError as exc:
            print("Template repair was refused: {}".format(exc))
            continue
        if repaired.valid:
            print("✓ Template repaired on the Notecard.")
            print(
                "  Important: sync this template to Notehub once over cellular or Wi-Fi before using it over NTN."
            )
        else:
            print("✗ The template still does not verify correctly:")
            print(_json(repaired.response))


def _sync_callback(update: SyncUpdate) -> None:
    print(
        "[{:>3}s] {:<15} {}".format(
            update.elapsed, update.phase.value, update.message
        )
    )


def _show_sync_result(result: SyncResult) -> None:
    if result.phase == SyncPhase.COMPLETED and result.requested:
        print("✓ The local Notecard sync cycle completed.")
        print(
            "  Confirm end-to-end delivery in Notehub; local completion alone is not satellite delivery proof."
        )
    elif result.phase == SyncPhase.COMPLETED and not result.requested:
        print("• An earlier sync resolved; this tool did not issue a duplicate request.")
        print(
            "  Blues does not report that sync's direction. Run this action again only if the desired data is still pending."
        )
    elif result.phase == SyncPhase.TIMED_OUT:
        print("! The local wait limit was reached.")
        print("  {}".format(result.error))
        print("  The queued request may still complete later; this is not a generic failure.")
    elif result.phase == SyncPhase.UNRESOLVED:
        print("! Sync status is unavailable and the request state is unresolved.")
        print("  {}".format(result.error))
    else:
        print("✗ Sync did not complete: {}".format(result.error or result.response))
    if result.completed and result.response.get("sync") is True:
        print(
            "! The status still reports sync=true, so unsynchronized work remains."
        )


def _monitor(client: NotecardClient, args: argparse.Namespace) -> SyncMonitor:
    return SyncMonitor(
        client,
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.sync_timeout,
    )


def full_diagnostic(
    client: NotecardClient, args: argparse.Namespace
) -> DiagnosticSnapshot:
    snapshot, template_checks = DiagnosticRunner(client).run()
    print_diagnostic(snapshot)
    _offer_template_repairs(client, template_checks)

    print(
        "\nThe checks above were read-only. An active inbound NTN check can consume satellite data."
    )
    if confirm("Run one active inbound sync now?"):
        result = _monitor(client, args).run("in", callback=_sync_callback)
        _show_sync_result(result)
        snapshot.raw["active_inbound_sync"] = {
            "requested_by_tool": result.requested,
            "phase": result.phase.value,
            "error": result.error,
            "response": result.response,
        }
        snapshot.checks.append(
            DiagnosticCheck(
                name="Active inbound sync",
                level=(
                    CheckLevel.PASS
                    if result.completed and result.response.get("sync") is not True
                    else CheckLevel.WARN
                ),
                summary=(
                    "Local sync cycle completed, but unsynchronized work remains."
                    if result.completed and result.response.get("sync") is True
                    else "Local sync cycle completed."
                    if result.completed
                    else "Did not reach local completion: {}".format(result.error)
                ),
                detail="Notehub event transport is still required for delivery proof.",
            )
        )
    return snapshot


def send_message(client: NotecardClient, args: argparse.Namespace) -> None:
    check = verify_template(client, OUTBOUND_TEMPLATE)
    if not check.valid:
        print("\nOutbound satellite template is not ready.")
        _describe_template(check)
        if not check.repairable:
            print(
                "Verification failed, so the current template is unknown. No overwrite or message queue action was attempted."
            )
            return
        if confirm("Apply this persistent template repair?"):
            try:
                repaired = repair_template(
                    client, OUTBOUND_TEMPLATE, expected=check
                )
            except SatphoneError as exc:
                print("Template repair was refused: {}".format(exc))
                print("Nothing was queued.")
                return
            if repaired.valid:
                print("✓ Template repaired.")
                print(
                    "Sync the new template once over cellular or Wi-Fi before sending over satellite. Message not queued."
                )
            else:
                print("Template repair did not verify: {}".format(repaired.response))
        else:
            print("Nothing changed and no message was queued.")
        return

    print("\nMessages are limited to {} UTF-8 bytes in this reference schema.".format(MAX_MESSAGE_BYTES))
    value = input("Message: ")
    try:
        response = queue_outbound_message(client, value)
    except ValueError as exc:
        print("Nothing sent: {}".format(exc))
        return
    except NotecardRequestError as exc:
        print("Message was rejected by the Notecard: {}".format(exc))
        print("No sync was requested.")
        return
    except SatphoneError as exc:
        print("Message queue state is uncertain: {}".format(exc))
        print("No sync was requested. Inspect messages.qo before trying again.")
        return
    print("✓ Message queued locally on the Notecard: {}".format(response))
    result = _monitor(client, args).run("out", callback=_sync_callback)
    _show_sync_result(result)


def _print_messages(messages: Iterable[Message]) -> None:
    messages = list(messages)
    if not messages:
        print("No incoming messages are stored locally.")
        return
    print("\n{} INCOMING MESSAGE(S)\n".format(len(messages)))
    for index, message in enumerate(messages, 1):
        timestamp = ""
        if message.time:
            try:
                timestamp = datetime.fromtimestamp(message.time).astimezone().isoformat()
            except (OSError, OverflowError, ValueError):
                timestamp = str(message.time)
        print("{}. {}".format(index, message.text))
        if timestamp:
            print("   Received: {}".format(timestamp))


def check_incoming(client: NotecardClient, args: argparse.Namespace) -> None:
    print("\nRequesting one inbound sync. Existing active work will be monitored, not duplicated.")
    result = _monitor(client, args).run("in", callback=_sync_callback)
    _show_sync_result(result)
    try:
        _print_messages(read_messages(client))
        print("Messages were read without deletion.")
    except Exception as exc:
        print("Could not read the local inbox: {}".format(exc))


def view_incoming(client: NotecardClient) -> None:
    print("\n1. Read only")
    print("2. Read and delete")
    print("0. Back")
    choice = input("> ").strip()
    if choice == "1":
        messages = read_messages(client)
        _print_messages(messages)
        print("Messages were not deleted.")
    elif choice == "2":
        current = read_messages(client)
        _print_messages(current)
        if not current:
            return
        if not confirm(
            "Delete local messages from messages.qi as they are read? This cannot be undone."
        ):
            print("Messages left unchanged.")
            return
        # Delete only the messages that were displayed and confirmed. New
        # arrivals after this point are intentionally left for the next view.
        try:
            deleted = read_and_delete_messages(
                client, maximum=len(current), expected=current
            )
        except MessageDeletionError as exc:
            print(
                "Deleted {} confirmed local incoming message(s) before the operation stopped.".format(
                    len(exc.deleted)
                )
            )
            if exc.uncertain_latest:
                print(
                    "The latest replay-safe transaction did not return a verifiable response, so up to {} additional Note(s) may have been removed. No destructive outer retry was attempted.".format(
                        exc.uncertain_count or 1
                    )
                )
            print("Reason: {}".format(exc))
            return
        print(
            "Deleted and response-verified {} local incoming message(s).".format(
                len(deleted)
            )
        )


def show_location(client: NotecardClient) -> None:
    location = inspect_location(client)
    configuration = location_configuration(client)
    location = apply_location_configuration(location, configuration)
    print("\nLOCATION\n")
    if location.known:
        print("Latitude:  {:.6f}".format(location.latitude))
        print("Longitude: {:.6f}".format(location.longitude))
        print("Source:    {}".format(location.source))
        print("Mode:      {}".format(location.mode or "not reported"))
        print("Captured:  {}".format(location.captured_at or "not reported"))
        print("DOP:       {}".format(location.dop if location.dop is not None else "not reported"))
        if location.status:
            print("Status:    {}".format(location.status))
    else:
        print("No usable current or last-known coordinates are available.")
        print("Status: {}".format(location.status or location.raw))
        print("Move outside with a clear view of the sky and wait for a real fix.")
        return

    current_mode = configuration["card.location.mode"]
    current_ntn_gps = configuration["ntn.gps"]
    configuration_error = location_configuration_error(configuration)
    if configuration_error:
        print("\nCurrent configuration could not be read safely:")
        print(_json(configuration))
        print("Reason: {}".format(configuration_error))
        print("No persistent location change was offered.")
        return
    if location.source not in ("GPS/GNSS", "fixed"):
        print(
            "\nThese coordinates are not proven GPS/GNSS data, so the tool will not offer to make them a fixed satellite location."
        )
        return
    if current_mode.get("mode") == "fixed" and current_ntn_gps.get("on"):
        print("\nThe Notecard is already fixed and StarNote is using its location.")
        return

    print("\nCurrent persistent configuration:")
    print(_json(configuration))
    print("\nProposed changes:")
    print(
        _json(
            [
                {
                    "req": "card.location.mode",
                    "mode": "fixed",
                    "lat": location.latitude,
                    "lon": location.longitude,
                },
                {"req": "ntn.gps", "on": True},
            ]
        )
    )
    if confirm("Use these exact verified coordinates as the fixed StarNote test location?"):
        response = set_fixed_test_location(client, location)
        print("✓ Fixed test location configured: {}".format(response))
    else:
        print("Location configuration left unchanged.")


def show_ntn_status(client: NotecardClient) -> None:
    response = ntn_status(client)
    print("\nSTARNOTE / NTN STATUS\n{}".format(_json(response)))
    tags = status_tags("{} {}".format(response.get("status", ""), response.get("err", "")))
    if "no-ntn-module" in tags:
        print("\nNo StarNote is connected or responding.")
    elif "ntn-idle" in tags:
        print("\nIdle is the documented ready state after pairing.")
    if "ntn-unknown-location" in tags:
        print("The StarNote does not currently know a usable location.")


def show_transport(client: NotecardClient) -> None:
    response = transport_status(client)
    print("\nTRANSPORT CONFIGURATION\n{}".format(_json(response)))
    current_error = transport_status_error(response)
    if current_error:
        print("\nCurrent transport could not be verified: {}".format(current_error))
        print("No persistent transport change was offered.")
        return
    if transport_supports_ntn(response):
        return
    print("\nThe current method does not include NTN.")
    print('Proposed persistent change: {"req":"card.transport","method":"ntn"}')
    if confirm("Set satellite-only NTN transport?"):
        result = set_ntn_transport(client)
        print("✓ Transport updated and verified: {}".format(result))
    else:
        print("Transport left unchanged.")


def _raw_debug_sync_preflight_error(client: NotecardClient) -> Optional[str]:
    hub_config = client.inspect({"req": "hub.get"})
    error = hub_sync_preflight_error(hub_config)
    if error:
        return error
    baseline = client.inspect({"req": "hub.sync.status"})
    error = sync_status_query_error(baseline)
    if error:
        return error
    if sync_status_is_active(baseline):
        return "an earlier sync appears active"
    return None


def live_debug(client: NotecardClient) -> None:
    print("\nRAW DEBUG MODE")
    print("Trace text is undocumented and is saved only for troubleshooting.")
    print("The log may contain device identifiers and precise location.")
    if not confirm("Enable trace mode and create a log file?"):
        return

    value = input("Capture duration in seconds [120]: ").strip()
    try:
        duration = int(value) if value else 120
        if duration < 1 or duration > 3600:
            raise ValueError
    except ValueError:
        print("Enter a duration from 1 to 3600 seconds.")
        return

    print("\n1. Monitor only")
    print("2. Request one inbound sync")
    print("3. Request one outbound sync")
    choice = input("> ").strip()
    direction = {"2": "in", "3": "out"}.get(choice)
    if direction:
        preflight_error = _raw_debug_sync_preflight_error(client)
        if preflight_error:
            print(
                "Debug mode will not send a sync request: {}.".format(
                    preflight_error
                )
            )
            direction = None
        elif direction and not confirm(
            "This sends one {}bound sync and may consume satellite data. Continue?".format(
                direction
            )
        ):
            direction = None
        elif direction:
            # The confirmation can sit open long enough for an automatic sync
            # to begin. Re-read immediately before trace takes the stream.
            preflight_error = _raw_debug_sync_preflight_error(client)
            if preflight_error:
                print(
                    "Debug mode will not send a sync request after the final "
                    "preflight: {}.".format(preflight_error)
                )
                direction = None

    print("\nCapturing trace; press Control-C to stop early.\n")
    try:
        path = capture_trace(
            client,
            duration_seconds=duration,
            sync_direction=direction,
            line_callback=print,
        )
    except KeyboardInterrupt:
        print("\nTrace stopped; cleanup is finishing.")
        # capture_trace's finally block already disabled trace.
        return
    print("\nTrace saved to {}".format(path))


def export_diagnostic(client: NotecardClient) -> DiagnosticSnapshot:
    snapshot, _ = DiagnosticRunner(client).run()
    path = export_report(
        snapshot,
        transaction_history=client.history,
        logs_dir=Path("logs"),
    )
    print("\nDiagnostic report saved to:")
    print(path)
    print(
        "Review it before sharing; it may contain identifiers, precise location, and message contents."
    )
    return snapshot


def print_menu() -> None:
    print(
        """
SATPHONE DEVELOPMENT TOOL

1. Run full diagnostic
2. Send satellite message
3. Check for incoming satellite message
4. View incoming messages
5. Check GPS/location
6. Show StarNote status
7. Show transport configuration
8. Live debug mode
9. Export diagnostic report
0. Exit
""".strip()
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local Blues Notecard/StarNote development harness"
    )
    parser.add_argument("--port", help="Explicit serial device, e.g. /dev/cu.usbmodemNOTE1")
    parser.add_argument(
        "--sync-timeout",
        type=float,
        default=900.0,
        help="Local sync wait limit in seconds (default: 900)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=10.0,
        help="hub.sync.status polling interval (default: 10)",
    )
    return parser


def interactive(client: NotecardClient, args: argparse.Namespace) -> int:
    actions = {
        "1": lambda: full_diagnostic(client, args),
        "2": lambda: send_message(client, args),
        "3": lambda: check_incoming(client, args),
        "4": lambda: view_incoming(client),
        "5": lambda: show_location(client),
        "6": lambda: show_ntn_status(client),
        "7": lambda: show_transport(client),
        "8": lambda: live_debug(client),
        "9": lambda: export_diagnostic(client),
    }
    while True:
        print_menu()
        choice = input("\n> ").strip()
        if choice == "0":
            return 0
        action = actions.get(choice)
        if action is None:
            print("Choose a number from 0 to 9.")
            continue
        try:
            action()
        except UnsafeSerialStateError as exc:
            print("\nUnsafe serial state: {}".format(exc))
            print("SATPHONE is exiting so no further request uses this connection.")
            return 1
        except KeyboardInterrupt:
            print("\nAction cancelled.")
        except Exception as exc:
            print("\nAction failed: {}".format(exc))


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if (
        not math.isfinite(args.sync_timeout)
        or not math.isfinite(args.poll_seconds)
        or args.sync_timeout <= 0
        or args.poll_seconds <= 0
    ):
        print("Sync timeout and poll interval must be positive.", file=sys.stderr)
        return 2

    print("SATPHONE DEVELOPMENT TOOL")
    print("Connecting over USB serial...")
    try:
        port = _choose_port(args.port)
        client = NotecardClient.open(port)
    except (EOFError, KeyboardInterrupt):
        print("\nConnection selection cancelled.", file=sys.stderr)
        return 130
    except SatphoneError as exc:
        print("\nCould not connect: {}".format(exc), file=sys.stderr)
        print(
            "Connect the Starter Kit by USB and disconnect the Blues browser terminal.",
            file=sys.stderr,
        )
        return 1

    print("✓ Notecard connected on {}".format(port))
    exit_code = 0
    try:
        exit_code = interactive(client, args)
    except (EOFError, KeyboardInterrupt):
        print("\nExiting.")
    finally:
        client.close()
    return exit_code
