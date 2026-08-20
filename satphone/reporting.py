"""Human-readable diagnostic report formatting and export."""

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .models import CheckLevel, DiagnosticSnapshot


LEVEL_LABELS = {
    CheckLevel.PASS: "PASS",
    CheckLevel.WARN: "WARNING",
    CheckLevel.FAIL: "FAIL",
    CheckLevel.INFO: "INFO",
}


def format_report(
    snapshot: DiagnosticSnapshot,
    transaction_history: Optional[Iterable[Dict[str, Any]]] = None,
) -> str:
    lines = [
        "SATPHONE DIAGNOSTIC REPORT",
        "",
        "Timestamp: {}".format(snapshot.timestamp.isoformat()),
        "Serial Port: {}".format(snapshot.serial_port),
        "",
        "SUMMARY",
    ]
    for check in snapshot.checks:
        lines.append(
            "[{}] {}: {}".format(LEVEL_LABELS[check.level], check.name, check.summary)
        )
        if check.detail:
            lines.append("  Detail: {}".format(check.detail))
        if check.recommendation:
            lines.append("  Recommended action: {}".format(check.recommendation))

    lines.extend(
        [
            "",
            "RAW RESPONSES",
            json.dumps(snapshot.raw, indent=2, sort_keys=True, default=str),
        ]
    )
    if transaction_history is not None:
        lines.extend(
            [
                "",
                "TRANSACTION HISTORY",
                json.dumps(list(transaction_history), indent=2, sort_keys=True, default=str),
            ]
        )
    lines.extend(
        [
            "",
            "Privacy note: this report may contain device identifiers, precise location, and message contents.",
        ]
    )
    return "\n".join(lines) + "\n"


def export_report(
    snapshot: DiagnosticSnapshot,
    transaction_history: Optional[Iterable[Dict[str, Any]]] = None,
    logs_dir: Path = Path("logs"),
) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = snapshot.timestamp.strftime("%Y-%m-%d-%H%M%S")
    report = format_report(snapshot, transaction_history=transaction_history)
    for index in range(1, 10000):
        suffix = "" if index == 1 else "-{}".format(index)
        path = logs_dir / "{}-diagnostic{}.txt".format(stamp, suffix)
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(report)
            return path.resolve()
        except FileExistsError:
            continue
    raise OSError("Could not allocate a unique diagnostic report filename")
