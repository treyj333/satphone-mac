"""Secure Discord slash-command bridge for queueing Notehub inbound Notes."""

import argparse
import asyncio
import getpass
import hashlib
import http.client
import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Optional, Sequence, Tuple
from urllib.parse import quote

from . import INBOUND_FILE
from .messages import validate_message


NOTEHUB_HOST = "api.notefile.net"
DISCORD_TOKEN_ENV = "SATPHONE_DISCORD_BOT_TOKEN"
NOTEHUB_TOKEN_ENV = "SATPHONE_NOTEHUB_TOKEN"
DISCORD_KEYCHAIN_SERVICE = "satphone-v0.discord-bot-token"
NOTEHUB_KEYCHAIN_SERVICE = "satphone-v0.notehub-personal-access-token"
KEYCHAIN_ACCOUNT = "satphone-v0"
MAX_RESPONSE_BYTES = 16 * 1024
MAX_INTERACTIONS_PER_USER_PER_MINUTE = 5
MAX_INTERACTIONS_PER_DEVICE_PER_MINUTE = 5
INTERACTION_RETENTION_SECONDS = 30 * 24 * 60 * 60
BLOCKED_RETENTION_SECONDS = 20 * 60
RECENT_DUPLICATE_SECONDS = 5 * 60
PLAIN_MESSAGE_GUIDANCE_COOLDOWN_SECONDS = 5 * 60
PLAIN_MESSAGE_GUIDANCE_TEXT = (
    "That regular Discord message was not forwarded to the Satphone. "
    "Use `/satphone` and fill in its `message` field. After Discord says it "
    "was queued, leave Auto receive on in the SATPHONE app or click Receive Now."
)


class BridgeConfigurationError(ValueError):
    """Raised when bridge configuration or credentials are incomplete."""


class QueueStatus(str, Enum):
    QUEUED = "queued"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"


class InteractionStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    UNCERTAIN = "uncertain"


class PlainMessageGuidanceLimiter:
    """Rate-limit metadata-only guidance for ordinary channel messages."""

    def __init__(
        self,
        config: "BridgeConfig",
        cooldown_seconds: float = PLAIN_MESSAGE_GUIDANCE_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self.cooldown_seconds = cooldown_seconds
        self.clock = clock
        self._last_reply_by_user: Dict[int, float] = {}

    def should_reply(
        self,
        guild_id: Optional[int],
        channel_id: Optional[int],
        user_id: Optional[int],
        author_is_bot: bool,
    ) -> bool:
        if author_is_bot or user_id is None:
            return False
        if guild_id != self.config.discord_guild_id:
            return False
        if channel_id != self.config.discord_channel_id:
            return False

        now = self.clock()
        previous = self._last_reply_by_user.get(user_id)
        if previous is not None and now - previous < self.cooldown_seconds:
            return False
        self._last_reply_by_user[user_id] = now
        return True


@dataclass(frozen=True)
class BridgeConfig:
    project_uid: str
    device_uid: str
    discord_guild_id: int
    discord_channel_id: int
    state_path: Path
    allowed_user_ids: FrozenSet[int] = frozenset()
    trust_discord_command_permissions: bool = False
    notefile: str = INBOUND_FILE

    @classmethod
    def load(cls, path: Path) -> "BridgeConfig":
        path = Path(path).expanduser().resolve()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise BridgeConfigurationError(
                "Discord bridge config was not found: {}".format(path)
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeConfigurationError(
                "Discord bridge config could not be read: {}".format(exc)
            ) from exc
        if not isinstance(raw, dict):
            raise BridgeConfigurationError("Discord bridge config must be a JSON object.")

        project_uid = _required_string(raw, "project_uid")
        device_uid = _required_string(raw, "device_uid")
        guild_id = _required_snowflake(raw, "discord_guild_id")
        channel_id = _required_snowflake(raw, "discord_channel_id")
        notefile = raw.get("notefile", INBOUND_FILE)
        if notefile != INBOUND_FILE:
            raise BridgeConfigurationError(
                "This bridge is locked to {} so its payload matches the verified "
                "inbound template.".format(INBOUND_FILE)
            )

        configured_state = raw.get("state_path", "logs/discord-bridge.sqlite3")
        if not isinstance(configured_state, str) or not configured_state.strip():
            raise BridgeConfigurationError("state_path must be a non-empty string.")
        state_path = Path(configured_state).expanduser()
        if not state_path.is_absolute():
            state_path = path.parent / state_path
        state_path = state_path.resolve()

        raw_users = raw.get("allowed_user_ids", [])
        if not isinstance(raw_users, list):
            raise BridgeConfigurationError("allowed_user_ids must be a JSON array.")
        users = frozenset(
            _snowflake(value, "allowed_user_ids") for value in raw_users
        )
        trust_discord_permissions = raw.get(
            "trust_discord_command_permissions", False
        )
        if not isinstance(trust_discord_permissions, bool):
            raise BridgeConfigurationError(
                "trust_discord_command_permissions must be true or false."
            )
        if not users and not trust_discord_permissions:
            raise BridgeConfigurationError(
                "Set at least one allowed_user_ids entry, or explicitly set "
                "trust_discord_command_permissions to true."
            )
        return cls(
            project_uid=project_uid,
            device_uid=device_uid,
            discord_guild_id=guild_id,
            discord_channel_id=channel_id,
            state_path=state_path,
            allowed_user_ids=users,
            trust_discord_command_permissions=trust_discord_permissions,
            notefile=notefile,
        )


@dataclass(frozen=True)
class QueueResult:
    status: QueueStatus
    http_status: Optional[int] = None


@dataclass(frozen=True)
class Reservation:
    created: bool
    status: InteractionStatus
    same_message: bool
    rate_limited: bool = False
    recent_duplicate: bool = False


@dataclass(frozen=True)
class BridgeReply:
    status: InteractionStatus
    text: str


def _required_string(raw: Dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BridgeConfigurationError("{} must be a non-empty string.".format(key))
    return value.strip()


def _snowflake(value: Any, field: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        number = value
    elif (
        isinstance(value, str)
        and value
        and value.isascii()
        and value.isdigit()
    ):
        number = int(value)
    else:
        raise BridgeConfigurationError("{} must contain Discord IDs.".format(field))
    if number <= 0 or number > (2**64 - 1):
        raise BridgeConfigurationError("{} must contain positive Discord IDs.".format(field))
    return number


def _required_snowflake(raw: Dict[str, Any], key: str) -> int:
    if key not in raw:
        raise BridgeConfigurationError("{} is required.".format(key))
    return _snowflake(raw[key], key)


def _message_digest(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


class InteractionLedger:
    """Durable at-most-once guard keyed by Discord interaction ID."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS discord_interactions (
                    interaction_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    message_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS discord_interactions_user_created
                ON discord_interactions (user_id, created_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS discord_interactions_created
                ON discord_interactions (created_at)
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path), timeout=10)

    def reserve(
        self,
        interaction_id: str,
        user_id: int,
        message_sha256: str,
        allow_duplicate: bool = False,
    ) -> Reservation:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                DELETE FROM discord_interactions
                WHERE (status = ? AND created_at < ?)
                   OR (status != ? AND created_at < ?)
                """,
                (
                    InteractionStatus.BLOCKED.value,
                    now - BLOCKED_RETENTION_SECONDS,
                    InteractionStatus.BLOCKED.value,
                    now - INTERACTION_RETENTION_SECONDS,
                ),
            )
            existing = connection.execute(
                """
                SELECT message_sha256, status
                FROM discord_interactions
                WHERE interaction_id = ?
                """,
                (str(interaction_id),),
            ).fetchone()
            if existing is not None:
                return Reservation(
                    created=False,
                    status=InteractionStatus(existing[1]),
                    same_message=existing[0] == message_sha256,
                )
            if not allow_duplicate:
                recent_duplicate = connection.execute(
                    """
                    SELECT status
                    FROM discord_interactions
                    WHERE user_id = ?
                      AND message_sha256 = ?
                      AND created_at > ?
                      AND status IN (?, ?, ?)
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (
                        str(user_id),
                        message_sha256,
                        now - RECENT_DUPLICATE_SECONDS,
                        InteractionStatus.PENDING.value,
                        InteractionStatus.QUEUED.value,
                        InteractionStatus.UNCERTAIN.value,
                    ),
                ).fetchone()
                if recent_duplicate is not None:
                    self._insert_blocked(
                        connection,
                        interaction_id,
                        user_id,
                        message_sha256,
                        now,
                    )
                    return Reservation(
                        created=False,
                        status=InteractionStatus.BLOCKED,
                        same_message=True,
                        recent_duplicate=True,
                    )
            recent_for_user = connection.execute(
                """
                SELECT COUNT(*)
                FROM discord_interactions
                WHERE user_id = ? AND created_at > ? AND status != ?
                """,
                (
                    str(user_id),
                    now - 60,
                    InteractionStatus.BLOCKED.value,
                ),
            ).fetchone()[0]
            recent_for_device = connection.execute(
                """
                SELECT COUNT(*)
                FROM discord_interactions
                WHERE created_at > ? AND status != ?
                """,
                (now - 60, InteractionStatus.BLOCKED.value),
            ).fetchone()[0]
            if (
                recent_for_user >= MAX_INTERACTIONS_PER_USER_PER_MINUTE
                or recent_for_device >= MAX_INTERACTIONS_PER_DEVICE_PER_MINUTE
            ):
                self._insert_blocked(
                    connection,
                    interaction_id,
                    user_id,
                    message_sha256,
                    now,
                )
                return Reservation(
                    created=False,
                    status=InteractionStatus.BLOCKED,
                    same_message=True,
                    rate_limited=True,
                )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO discord_interactions
                    (interaction_id, user_id, message_sha256, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(interaction_id),
                    str(user_id),
                    message_sha256,
                    InteractionStatus.PENDING.value,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT message_sha256, status
                FROM discord_interactions
                WHERE interaction_id = ?
                """,
                (str(interaction_id),),
            ).fetchone()
        if row is None:
            raise RuntimeError("interaction reservation could not be read back")
        return Reservation(
            created=cursor.rowcount == 1,
            status=InteractionStatus(row[1]),
            same_message=row[0] == message_sha256,
        )

    @staticmethod
    def _insert_blocked(
        connection: sqlite3.Connection,
        interaction_id: str,
        user_id: int,
        message_sha256: str,
        now: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO discord_interactions
                (interaction_id, user_id, message_sha256, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(interaction_id),
                str(user_id),
                message_sha256,
                InteractionStatus.BLOCKED.value,
                now,
                now,
            ),
        )

    def mark(self, interaction_id: str, status: InteractionStatus) -> None:
        if status == InteractionStatus.PENDING:
            raise ValueError("pending is reserved only at insertion")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE discord_interactions
                SET status = ?, updated_at = ?
                WHERE interaction_id = ?
                """,
                (status.value, int(time.time()), str(interaction_id)),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("interaction reservation disappeared")


NotehubTransport = Callable[
    [str, bytes, Dict[str, str], float], Tuple[int, bytes]
]


class NotehubInboundClient:
    """One-attempt client for Notehub's Add QI Note API."""

    def __init__(
        self,
        project_uid: str,
        device_uid: str,
        token: str,
        notefile: str = INBOUND_FILE,
        timeout_seconds: float = 15.0,
        transport: Optional[NotehubTransport] = None,
    ):
        self.project_uid = project_uid
        self.device_uid = device_uid
        self.notefile = notefile
        self._token = token
        self.timeout_seconds = timeout_seconds
        self._transport = transport or self._send

    @property
    def path(self) -> str:
        segments = (
            self.project_uid,
            self.device_uid,
            self.notefile,
        )
        return "/v1/projects/{}/devices/{}/notes/{}".format(
            *(quote(segment, safe="") for segment in segments)
        )

    @staticmethod
    def _send(
        path: str,
        body: bytes,
        headers: Dict[str, str],
        timeout_seconds: float,
    ) -> Tuple[int, bytes]:
        connection = http.client.HTTPSConnection(
            NOTEHUB_HOST, timeout=timeout_seconds
        )
        try:
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_RESPONSE_BYTES:
                return response.status, b"__response_too_large__"
            return response.status, response_body
        finally:
            connection.close()

    def enqueue(self, message: str) -> QueueResult:
        text = validate_message(message)
        body = json.dumps(
            {"body": {"msg": text}},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "Authorization": "Bearer {}".format(self._token),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "satphone-discord-bridge/1.0",
        }
        try:
            status, response_body = self._transport(
                self.path, body, headers, self.timeout_seconds
            )
        except (Exception, KeyboardInterrupt):
            # Add QI Note is not idempotent. A timeout or disconnect may have
            # occurred after Notehub accepted it, so no outer retry is safe.
            return QueueResult(QueueStatus.UNCERTAIN)

        if 200 <= status < 300:
            stripped = response_body.strip()
            if not stripped:
                return QueueResult(QueueStatus.QUEUED, status)
            try:
                decoded = json.loads(stripped.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return QueueResult(QueueStatus.UNCERTAIN, status)
            if decoded == {}:
                return QueueResult(QueueStatus.QUEUED, status)
            return QueueResult(QueueStatus.UNCERTAIN, status)

        if 400 <= status < 500 and status != 408:
            return QueueResult(QueueStatus.REJECTED, status)
        return QueueResult(QueueStatus.UNCERTAIN, status)


class DiscordToNotehubService:
    """Hardware-independent authorization, deduplication, and queue service."""

    def __init__(
        self,
        config: BridgeConfig,
        ledger: InteractionLedger,
        notehub: NotehubInboundClient,
        queued_callback: Optional[Callable[[str], None]] = None,
    ):
        self.config = config
        self.ledger = ledger
        self.notehub = notehub
        self.queued_callback = queued_callback

    def queue_from_interaction(
        self,
        interaction_id: Any,
        guild_id: Optional[int],
        channel_id: Optional[int],
        user_id: int,
        message: str,
        allow_duplicate: bool = False,
    ) -> BridgeReply:
        if guild_id != self.config.discord_guild_id:
            return BridgeReply(
                InteractionStatus.REJECTED,
                "This command is limited to the configured Satphone server.",
            )
        if channel_id != self.config.discord_channel_id:
            return BridgeReply(
                InteractionStatus.REJECTED,
                "Use this command in the configured Satphone channel.",
            )
        if (
            self.config.allowed_user_ids
            and user_id not in self.config.allowed_user_ids
        ):
            return BridgeReply(
                InteractionStatus.REJECTED,
                "You are not authorized to queue Satphone messages.",
            )
        try:
            text = validate_message(message)
        except (TypeError, ValueError) as exc:
            return BridgeReply(InteractionStatus.REJECTED, str(exc))

        interaction_key = str(interaction_id)
        digest = _message_digest(text)
        reservation = self.ledger.reserve(
            interaction_key,
            user_id,
            digest,
            allow_duplicate=allow_duplicate,
        )
        if not reservation.created:
            if reservation.rate_limited:
                return BridgeReply(
                    InteractionStatus.REJECTED,
                    "Rate limit reached: wait one minute before queueing another "
                    "Satphone message.",
                )
            if reservation.recent_duplicate:
                return BridgeReply(
                    InteractionStatus.REJECTED,
                    "An identical message from you was already attempted in the "
                    "last five minutes. Nothing new was queued. Check Notehub, or "
                    "use allow_duplicate only when a second copy is intentional.",
                )
            if not reservation.same_message:
                return BridgeReply(
                    InteractionStatus.UNCERTAIN,
                    "Discord reused an interaction ID with different content; "
                    "nothing new was queued.",
                )
            if reservation.status == InteractionStatus.QUEUED:
                return BridgeReply(
                    InteractionStatus.QUEUED,
                    "This Discord interaction was already queued; no duplicate "
                    "Note was created.",
                )
            if reservation.status in (
                InteractionStatus.REJECTED,
                InteractionStatus.BLOCKED,
            ):
                return BridgeReply(
                    InteractionStatus.REJECTED,
                    "This Discord interaction was already rejected; no duplicate "
                    "request was sent.",
                )
            return BridgeReply(
                InteractionStatus.UNCERTAIN,
                "This interaction is already pending or has an uncertain outcome. "
                "No duplicate request was sent; check Notehub before trying again.",
            )

        try:
            result = self.notehub.enqueue(text)
        except (Exception, KeyboardInterrupt):
            result = QueueResult(QueueStatus.UNCERTAIN)

        if result.status == QueueStatus.QUEUED:
            final_status = InteractionStatus.QUEUED
            reply = (
                "Queued in Notehub for messages.qi. The SATPHONE app will request "
                "the inbound sync when Auto receive is on; otherwise click Receive "
                "Now (legacy tool: option 3). This is not a delivery confirmation."
            )
        elif result.status == QueueStatus.REJECTED:
            final_status = InteractionStatus.REJECTED
            suffix = (
                " (HTTP {})".format(result.http_status)
                if result.http_status is not None
                else ""
            )
            reply = (
                "Notehub rejected the queue request{}; nothing was reported as "
                "queued."
            ).format(suffix)
        else:
            final_status = InteractionStatus.UNCERTAIN
            reply = (
                "Notehub did not return a trustworthy final result. Do not resend "
                "blindly; check the messages.qi queue or project events first."
            )
        try:
            self.ledger.mark(interaction_key, final_status)
        except Exception:
            # A surviving pending reservation still prevents an unsafe replay.
            return BridgeReply(
                InteractionStatus.UNCERTAIN,
                "The queue attempt finished but its duplicate guard could not be "
                "finalized. Do not retry until Notehub has been checked.",
            )
        if final_status == InteractionStatus.QUEUED and self.queued_callback:
            try:
                self.queued_callback(text)
            except Exception:
                # Notehub already accepted the Note. A local UI notification
                # must never make Discord treat that success as retryable.
                pass
        return BridgeReply(final_status, reply)


def _keychain_read(service: str) -> Optional[str]:
    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                KEYCHAIN_ACCOUNT,
                "-s",
                service,
                "-w",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _keychain_write(service: str, secret: str) -> None:
    # macOS explicitly warns that `-w <password>` exposes the password in the
    # child process's arguments. Ending the command with bare `-w` invokes the
    # security tool's two-entry prompt; feed those prompts over stdin so the
    # secret never appears in argv.
    result = subprocess.run(
        [
            "/usr/bin/security",
            "add-generic-password",
            "-U",
            "-a",
            KEYCHAIN_ACCOUNT,
            "-s",
            service,
            "-w",
        ],
        check=False,
        capture_output=True,
        text=True,
        input="{}\n{}\n".format(secret, secret),
        # Detach the child from the caller's controlling TTY. Otherwise macOS
        # security's getpass() ignores this stdin pipe and waits invisibly on
        # /dev/tty while stderr is captured.
        start_new_session=True,
    )
    if result.returncode != 0:
        raise BridgeConfigurationError(
            "macOS Keychain rejected the credential update."
        )


def load_secret(environment_name: str, keychain_service: str) -> str:
    value = os.environ.get(environment_name) or _keychain_read(keychain_service)
    if not value:
        raise BridgeConfigurationError(
            "{} is missing. Run `python discord_bridge.py configure` to store it "
            "in macOS Keychain.".format(environment_name)
        )
    if "\n" in value or "\r" in value:
        raise BridgeConfigurationError(
            "{} contains an invalid line break.".format(environment_name)
        )
    return value


def store_secrets(discord_token: str, notehub_token: str) -> None:
    """Validate and store both bridge credentials in macOS Keychain."""
    discord_token = str(discord_token).strip()
    notehub_token = str(notehub_token).strip()
    if not discord_token or "\n" in discord_token or "\r" in discord_token:
        raise BridgeConfigurationError("Enter a valid Discord bot token.")
    if not notehub_token or "\n" in notehub_token or "\r" in notehub_token:
        raise BridgeConfigurationError("Enter a valid Notehub personal access token.")
    _keychain_write(DISCORD_KEYCHAIN_SERVICE, discord_token)
    _keychain_write(NOTEHUB_KEYCHAIN_SERVICE, notehub_token)


def configure_secrets() -> int:
    print("Credentials are stored in macOS Keychain and are never written to this folder.")
    discord_token = getpass.getpass("Discord bot token: ").strip()
    if not discord_token:
        raise BridgeConfigurationError("Discord bot token cannot be empty.")
    notehub_token = getpass.getpass("Notehub personal access token: ").strip()
    if not notehub_token:
        raise BridgeConfigurationError("Notehub token cannot be empty.")
    store_secrets(discord_token, notehub_token)
    print("Discord and Notehub credentials are stored in macOS Keychain.")
    return 0


def check_setup(config_path: Path) -> int:
    config = BridgeConfig.load(config_path)
    load_secret(DISCORD_TOKEN_ENV, DISCORD_KEYCHAIN_SERVICE)
    load_secret(NOTEHUB_TOKEN_ENV, NOTEHUB_KEYCHAIN_SERVICE)
    InteractionLedger(config.state_path)
    print(
        "Local bridge files and credentials are present for Discord server {} "
        "channel {} and device {}.".format(
            config.discord_guild_id,
            config.discord_channel_id,
            config.device_uid,
        )
    )
    print(
        "This local check does not validate either token, the Discord guild "
        "installation, or Notehub write permission."
    )
    if config.trust_discord_command_permissions:
        print(
            "Authorization note: the local user allowlist is disabled; Discord's "
            "administrator-only command permissions are trusted."
        )
    return 0


def run_discord_bot(
    config: BridgeConfig,
    service: DiscordToNotehubService,
    token: str,
    register_commands: bool = False,
    stop_event: Optional[Any] = None,
    status_callback: Optional[Callable[[str], None]] = None,
) -> None:
    try:
        import discord
        from discord import app_commands
    except ImportError as exc:
        raise BridgeConfigurationError(
            "discord.py is not installed; reinstall requirements.txt."
        ) from exc

    guild = discord.Object(id=config.discord_guild_id)
    plain_message_guidance = PlainMessageGuidanceLimiter(config)

    def report_status(value: str) -> None:
        if status_callback is None:
            return
        try:
            status_callback(value)
        except Exception:
            pass

    class SatphoneDiscordClient(discord.Client):
        def __init__(self) -> None:
            intents = discord.Intents.none()
            intents.guild_messages = True
            super().__init__(
                intents=intents,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self.tree = app_commands.CommandTree(self)
            self._stop_task: Optional[asyncio.Task[Any]] = None

            @app_commands.guild_only()
            @app_commands.default_permissions()
            @app_commands.describe(
                message="Message for the Satphone (160 UTF-8 bytes max)",
                allow_duplicate=(
                    "Queue an intentional second copy of a recently attempted message"
                ),
            )
            async def satphone(
                interaction: discord.Interaction,
                message: app_commands.Range[str, 1, 160],
                allow_duplicate: bool = False,
            ) -> None:
                await interaction.response.defer(ephemeral=True, thinking=True)
                outcome_label = "internal-error"
                try:
                    reply = await asyncio.to_thread(
                        service.queue_from_interaction,
                        interaction.id,
                        interaction.guild_id,
                        interaction.channel_id,
                        interaction.user.id,
                        message,
                        allow_duplicate,
                    )
                    content = reply.text
                    outcome_label = reply.status.value
                except (Exception, KeyboardInterrupt):
                    content = (
                        "The bridge hit an internal error before it could report a "
                        "trustworthy result. Check Notehub before retrying."
                    )
                for attempt in range(2):
                    try:
                        await interaction.edit_original_response(
                            content=content,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        break
                    except Exception:
                        if attempt == 0:
                            await asyncio.sleep(0.5)
                            continue
                        print(
                            "Discord response update failed for interaction {} "
                            "after ledger outcome {}. Check Notehub before a new "
                            "command is sent.".format(
                                interaction.id,
                                outcome_label,
                            ),
                            file=sys.stderr,
                        )

            self.tree.add_command(
                app_commands.Command(
                    name="satphone",
                    description="Queue a message for the Satphone's next inbound sync",
                    callback=satphone,
                ),
                guild=guild,
            )

        async def on_message(self, message: Any) -> None:
            guild_id = getattr(getattr(message, "guild", None), "id", None)
            channel_id = getattr(getattr(message, "channel", None), "id", None)
            author = getattr(message, "author", None)
            user_id = getattr(author, "id", None)
            author_is_bot = bool(getattr(author, "bot", False))
            if not plain_message_guidance.should_reply(
                guild_id,
                channel_id,
                user_id,
                author_is_bot,
            ):
                return
            try:
                await message.reply(
                    PLAIN_MESSAGE_GUIDANCE_TEXT,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as exc:
                print(
                    "Discord could not post plain-message guidance in channel "
                    "{} (HTTP {}). Grant the bot View Channel and Send Messages "
                    "permissions there.".format(
                        config.discord_channel_id,
                        getattr(exc, "status", "unknown"),
                    ),
                    file=sys.stderr,
                )

        async def setup_hook(self) -> None:
            if stop_event is not None and not register_commands:
                self._stop_task = asyncio.create_task(self._watch_for_stop())
            if not register_commands:
                return
            existing = await self.tree.fetch_commands(guild=guild)
            unexpected = [
                command.name
                for command in existing
                if command.name != "satphone"
                or command.type != discord.AppCommandType.chat_input
            ]
            if unexpected:
                raise BridgeConfigurationError(
                    "The Discord application already owns other commands in this "
                    "guild ({}). Refusing a bulk overwrite; use the dedicated "
                    "Satphone Bridge application.".format(", ".join(sorted(unexpected)))
                )
            await self.tree.sync(guild=guild)

        async def _watch_for_stop(self) -> None:
            await asyncio.to_thread(stop_event.wait)
            report_status("stopping")
            await self.close()

        async def on_ready(self) -> None:
            if register_commands:
                print(
                    "Registered the guild-only /satphone command for Discord "
                    "server {}.".format(config.discord_guild_id)
                )
                await self.close()
                return
            report_status("online")
            print(
                "Discord bridge is online. Use /satphone in channel {}.".format(
                    config.discord_channel_id
                )
            )

        async def on_disconnect(self) -> None:
            if not self.is_closed():
                report_status("reconnecting")

        async def on_resumed(self) -> None:
            report_status("online")

    try:
        report_status("starting")
        SatphoneDiscordClient().run(token, log_handler=None)
    except BridgeConfigurationError:
        raise
    except discord.LoginFailure as exc:
        raise BridgeConfigurationError(
            "Discord rejected the bot token. Replace the Keychain credential."
        ) from exc
    except discord.Forbidden as exc:
        raise BridgeConfigurationError(
            "Discord denied the guild command or connection request. Confirm the "
            "dedicated app is installed in the configured server."
        ) from exc
    except discord.HTTPException as exc:
        raise BridgeConfigurationError(
            "Discord setup failed with HTTP status {}.".format(exc.status)
        ) from exc
    except (OSError, asyncio.TimeoutError) as exc:
        raise BridgeConfigurationError(
            "Discord could not be reached; check this Mac's network connection."
        ) from exc
    except discord.DiscordException as exc:
        raise BridgeConfigurationError(
            "Discord startup failed ({}).".format(type(exc).__name__)
        ) from exc
    finally:
        report_status("stopped")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bridge a restricted Discord slash command to Notehub messages.qi."
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=("run", "configure", "check", "register"),
        default="run",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("discord-bridge.json"),
        help="Path to non-secret bridge configuration.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "configure":
            return configure_secrets()
        if args.action == "check":
            return check_setup(args.config)

        config = BridgeConfig.load(args.config)
        discord_token = load_secret(DISCORD_TOKEN_ENV, DISCORD_KEYCHAIN_SERVICE)
        notehub_token = load_secret(NOTEHUB_TOKEN_ENV, NOTEHUB_KEYCHAIN_SERVICE)
        ledger = InteractionLedger(config.state_path)
        notehub = NotehubInboundClient(
            project_uid=config.project_uid,
            device_uid=config.device_uid,
            token=notehub_token,
            notefile=config.notefile,
        )
        service = DiscordToNotehubService(config, ledger, notehub)
        run_discord_bot(
            config,
            service,
            discord_token,
            register_commands=args.action == "register",
        )
        return 0
    except BridgeConfigurationError as exc:
        print("Discord bridge setup error: {}".format(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nDiscord bridge stopped.")
        return 130
    except (OSError, sqlite3.Error):
        print(
            "Discord bridge setup error: local state storage is unavailable.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
