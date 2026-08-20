"""macOS-friendly writable paths and first-run configuration migration."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Iterable, Optional


APP_NAME = "SATPHONE"


def app_support_dir() -> Path:
    """Return the per-user writable application directory."""
    override = os.environ.get("SATPHONE_APP_SUPPORT")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / "Library" / "Application Support" / APP_NAME).resolve()


def ensure_app_directories() -> Path:
    base = app_support_dir()
    for path in (base, base / "logs", base / "reports"):
        path.mkdir(parents=True, exist_ok=True)
    return base


def app_log_path(name: str = "satphone-app.log") -> Path:
    return ensure_app_directories() / "logs" / name


def bridge_config_path() -> Path:
    return ensure_app_directories() / "discord-bridge.json"


def bridge_state_path() -> Path:
    return ensure_app_directories() / "discord-bridge.sqlite3"


def resource_root() -> Path:
    """Return the source/resource root in development and frozen builds."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root).resolve()
    return Path(__file__).resolve().parent.parent


def _migration_candidates(extra: Optional[Iterable[Path]] = None) -> Iterable[Path]:
    seen = set()
    candidates = []
    explicit = os.environ.get("SATPHONE_BRIDGE_CONFIG")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    if extra:
        candidates.extend(Path(path).expanduser() for path in extra)
    candidates.extend(
        [
            Path.cwd() / "discord-bridge.json",
            resource_root() / "discord-bridge.json",
        ]
    )
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved not in seen:
            seen.add(resolved)
            yield resolved


def migrate_bridge_config(extra_candidates: Optional[Iterable[Path]] = None) -> Path:
    """Copy an existing non-secret bridge config into Application Support once.

    The migration deliberately rewrites only the local SQLite state path. Tokens
    never live in this JSON file and remain in macOS Keychain.
    """
    destination = bridge_config_path()
    if destination.exists():
        return destination

    for candidate in _migration_candidates(extra_candidates):
        if candidate == destination or not candidate.is_file():
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        raw["state_path"] = str(bridge_state_path())
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(raw, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, destination)
        return destination
    return destination


def bundled_example_config() -> Optional[Path]:
    candidate = resource_root() / "discord-bridge.example.json"
    return candidate if candidate.is_file() else None


def copy_example_config() -> Path:
    destination = bridge_config_path()
    source = bundled_example_config()
    if source is None:
        raise FileNotFoundError("The bundled Discord example configuration is missing.")
    shutil.copyfile(source, destination)
    return destination
