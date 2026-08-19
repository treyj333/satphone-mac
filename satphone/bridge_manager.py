"""A stoppable in-process Discord bridge for the desktop application."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional

from .discord_bridge import (
    BridgeConfig,
    DiscordToNotehubService,
    DISCORD_KEYCHAIN_SERVICE,
    DISCORD_TOKEN_ENV,
    InteractionLedger,
    NOTEHUB_KEYCHAIN_SERVICE,
    NOTEHUB_TOKEN_ENV,
    NotehubInboundClient,
    load_secret,
    run_discord_bot,
)


class BridgeManager:
    def __init__(
        self,
        status_callback: Optional[Callable[[str], None]] = None,
        queued_callback: Optional[Callable[[str], None]] = None,
        error_callback: Optional[Callable[[str], None]] = None,
    ):
        self.status_callback = status_callback
        self.queued_callback = queued_callback
        self.error_callback = error_callback
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._guard = threading.RLock()
        self._config_path: Optional[Path] = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _status(self, value: str) -> None:
        if self.status_callback:
            self.status_callback(value)

    def _error(self, value: str) -> None:
        if self.error_callback:
            self.error_callback(value)

    def start(self, config_path: Path) -> bool:
        with self._guard:
            if self.running:
                return False
            self._config_path = Path(config_path).expanduser().resolve()
            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=self._run,
                name="satphone-discord-bridge",
                daemon=True,
            )
            self._thread.start()
            return True

    def _run(self) -> None:
        try:
            config = BridgeConfig.load(self._config_path or Path("discord-bridge.json"))
            discord_token = load_secret(DISCORD_TOKEN_ENV, DISCORD_KEYCHAIN_SERVICE)
            notehub_token = load_secret(NOTEHUB_TOKEN_ENV, NOTEHUB_KEYCHAIN_SERVICE)
            ledger = InteractionLedger(config.state_path)
            notehub = NotehubInboundClient(
                project_uid=config.project_uid,
                device_uid=config.device_uid,
                token=notehub_token,
                notefile=config.notefile,
            )
            service = DiscordToNotehubService(
                config,
                ledger,
                notehub,
                queued_callback=self.queued_callback,
            )
            run_discord_bot(
                config,
                service,
                discord_token,
                stop_event=self._stop_event,
                status_callback=self._status,
            )
        except Exception as exc:
            self._status("error")
            self._error(str(exc))
        finally:
            self._status("stopped")

    def stop(self, timeout: float = 8.0) -> bool:
        with self._guard:
            thread = self._thread
            if not thread or not thread.is_alive():
                return True
            self._stop_event.set()
        thread.join(timeout=max(0.0, float(timeout)))
        return not thread.is_alive()

    def restart(self) -> bool:
        with self._guard:
            config_path = self._config_path
        if config_path is None:
            return False
        if not self.stop():
            self._error("The previous Discord connection did not stop in time.")
            return False
        return self.start(config_path)

    @staticmethod
    def register_commands(config_path: Path) -> None:
        config = BridgeConfig.load(Path(config_path))
        discord_token = load_secret(DISCORD_TOKEN_ENV, DISCORD_KEYCHAIN_SERVICE)
        notehub_token = load_secret(NOTEHUB_TOKEN_ENV, NOTEHUB_KEYCHAIN_SERVICE)
        ledger = InteractionLedger(config.state_path)
        service = DiscordToNotehubService(
            config,
            ledger,
            NotehubInboundClient(
                project_uid=config.project_uid,
                device_uid=config.device_uid,
                token=notehub_token,
                notefile=config.notefile,
            ),
        )
        run_discord_bot(
            config,
            service,
            discord_token,
            register_commands=True,
        )
