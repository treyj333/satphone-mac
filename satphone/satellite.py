"""StarNote status interpretation and conservative satellite sync control."""

import re
import time
from typing import Any, Callable, Dict, List, Optional, Set

from .models import SyncPhase, SyncResult, SyncUpdate
from .notecard import NotecardClient, SatphoneError


STATUS_TAG_PATTERN = re.compile(r"\{([^{}]+)\}")
MAX_UNCORRELATED_REQUEST_AGE_SECONDS = 900
MANUAL_SYNC_MODES = {"continuous", "minimum", "periodic"}

COMPLETION_TAGS = {"no-changes", "sync-end", "sync-completed"}
FAILURE_TAGS = {
    "auth",
    "connect-aborted",
    "connect-failure",
    "connect-ll-failure",
    "device-disabled",
    "extended-network-failure",
    "extended-service-failure",
    "host-unreachable",
    "hub-not-connected",
    "network-timeout",
    "no-handler",
    "no-ntn-module",
    "no-session",
    "notehub-open-failure",
    "product-noexist",
    "receive-timeout",
    "registration-failure",
    "request-failure",
    "service",
    "socket-connect-error",
    "socket-dns-failure",
    "socket-invalid-cert",
    "socket-tls-error",
    "sync-error",
    "sync-local-error",
    "sync-remote-error",
    "transport-unreachable",
}
CONNECTING_TAGS = {
    "connected",
    "connecting",
    "ntn-connected",
    "ntn-connecting",
    "ntn-initializing",
    "ntn-power",
}
SYNCHRONIZING_TAGS = {
    "sync",
    "sync-begin",
    "sync-disconnecting",
    "sync-get-local-changes",
    "sync-get-remote-changes",
    "sync-notehub",
}
WAITING_TAGS = {
    "auth-retry",
    "cell-registration-wait",
    "host-retry",
    "joining-network",
    "network-down",
    "network-error-wait",
    "ticket",
    "wait-data",
    "wait-gateway",
    "wait-module",
    "wait-network",
    "wait-service",
    "waiting",
    "wifi-join-wait",
}


def status_tags(value: Any) -> Set[str]:
    return {match.lower() for match in STATUS_TAG_PATTERN.findall(str(value or ""))}


def ntn_status(client: NotecardClient) -> Dict[str, Any]:
    return client.inspect({"req": "ntn.status"})


def transport_status(client: NotecardClient) -> Dict[str, Any]:
    return client.inspect({"req": "card.transport"})


def transport_supports_ntn(response: Dict[str, Any]) -> bool:
    method = str(response.get("method", "")).lower()
    return "ntn" in method.split("-")


def transport_status_error(response: Dict[str, Any]) -> Optional[str]:
    if response.get("err"):
        return str(response.get("err"))
    if not isinstance(response.get("method"), str) or not response.get("method"):
        return "card.transport did not return a method"
    return None


def set_ntn_transport(client: NotecardClient) -> Dict[str, Dict[str, Any]]:
    """Apply an already-confirmed NTN-only transport change and verify it."""
    current = transport_status(client)
    current_error = transport_status_error(current)
    if current_error:
        raise SatphoneError(
            "Current transport could not be verified, so no change was made: {}".format(
                current_error
            )
        )
    try:
        applied = client.request({"req": "card.transport", "method": "ntn"})
    except (Exception, KeyboardInterrupt) as exc:
        raise SatphoneError(
            "The card.transport write did not return a final response and may "
            "have applied. Re-read card.transport before retrying: {}".format(exc)
        ) from exc
    try:
        verified = transport_status(client)
    except (Exception, KeyboardInterrupt) as exc:
        raise SatphoneError(
            "The transport write returned, but verification was interrupted or "
            "failed. Re-read card.transport before retrying: {}".format(exc)
        ) from exc
    if transport_status_error(verified) or verified.get("method") != "ntn":
        raise SatphoneError(
            "The transport write returned, but method=ntn was not verified. "
            "Inspect card.transport before retrying: {}".format(verified)
        )
    return {"applied": applied, "verified": verified}


def hub_sync_preflight_error(response: Dict[str, Any]) -> Optional[str]:
    """Require known Notehub configuration before a potentially costly sync."""
    if response.get("err"):
        return "hub.get failed: {}".format(response.get("err"))
    product = response.get("product")
    if not isinstance(product, str) or not product.strip():
        return "hub.get did not return a ProductUID"
    mode = response.get("mode")
    if not isinstance(mode, str) or not mode:
        return "hub.get did not return a mode"
    lowered = mode.lower()
    if lowered in {"off", "dfu"}:
        return "Notehub mode '{}' ignores manual hub.sync requests".format(lowered)
    if lowered not in MANUAL_SYNC_MODES:
        return "Notehub mode '{}' is not recognized as sync-capable".format(mode)
    return None


def sync_status_query_error(response: Dict[str, Any]) -> Optional[str]:
    """Return a baseline query error that makes duplicate suppression unknown."""
    if response.get("err"):
        return "hub.sync.status failed: {}".format(response.get("err"))
    if "status" in response and not isinstance(response.get("status"), str):
        return "hub.sync.status returned a non-string status"
    for field in ("alert", "sync"):
        if field in response and not isinstance(response.get(field), bool):
            return "hub.sync.status returned a non-boolean {}".format(field)
    for field in ("requested", "completed", "time", "seconds"):
        value = response.get(field)
        if field in response and (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            return "hub.sync.status returned an invalid {} counter".format(field)
    return None


def classify_sync_status(response: Dict[str, Any]) -> SyncPhase:
    if response.get("err"):
        return SyncPhase.FAILED

    status = str(response.get("status", ""))
    lowered = status.lower()
    tags = status_tags(status)
    if tags & FAILURE_TAGS:
        return SyncPhase.FAILED
    # alert describes the most recent sync and can remain true while a new
    # explicitly transient status is already underway. Preserve that active
    # state for duplicate suppression, but never let alert+terminal look like
    # a successful completion.
    if response.get("alert") is True:
        if tags & SYNCHRONIZING_TAGS or "synchroniz" in lowered:
            return SyncPhase.SYNCHRONIZING
        if tags & CONNECTING_TAGS or "connecting" in lowered:
            return SyncPhase.CONNECTING
        if tags & WAITING_TAGS or "waiting" in lowered or "searching" in lowered:
            return SyncPhase.WAITING
        return SyncPhase.FAILED
    if tags & COMPLETION_TAGS:
        return SyncPhase.COMPLETED
    if tags & SYNCHRONIZING_TAGS or "synchroniz" in lowered:
        return SyncPhase.SYNCHRONIZING
    if tags & CONNECTING_TAGS or "connecting" in lowered:
        return SyncPhase.CONNECTING
    if tags & WAITING_TAGS or "waiting" in lowered or "searching" in lowered:
        return SyncPhase.WAITING
    if "requested" in response:
        return SyncPhase.REQUESTED
    return SyncPhase.IDLE


def sync_status_is_active(response: Dict[str, Any]) -> bool:
    """Conservative inference; Blues exposes no documented active boolean."""
    phase = classify_sync_status(response)
    requested = response.get("requested")
    completed = response.get("completed")
    requested_is_age = isinstance(requested, (int, float)) and not isinstance(
        requested, bool
    )
    completed_is_age = isinstance(completed, (int, float)) and not isinstance(
        completed, bool
    )
    request_is_newer = requested_is_age and completed_is_age and requested < completed

    # When both ages are available, a newer completion is strong evidence that
    # a textual connecting/waiting status is stale. Otherwise a current
    # transient status is treated conservatively to suppress costly duplicates.
    if phase in {
        SyncPhase.WAITING,
        SyncPhase.CONNECTING,
        SyncPhase.SYNCHRONIZING,
    }:
        if requested_is_age and completed_is_age:
            # Equal second-resolution ages are ambiguous. Only a strictly
            # newer completion (smaller completed age) proves this status stale.
            return not completed < requested
        return True
    # The status string can describe the previous sync. A completion tag paired
    # with a strictly newer explicit request means that newer request is still
    # unresolved, so suppress another potentially costly request.
    if phase == SyncPhase.COMPLETED and request_is_newer:
        return True
    if phase != SyncPhase.REQUESTED:
        return False
    if completed_is_age:
        return request_is_newer or (
            requested == completed
            and 0 <= requested <= MAX_UNCORRELATED_REQUEST_AGE_SECONDS
        )
    return (
        requested_is_age
        and 0 <= requested <= MAX_UNCORRELATED_REQUEST_AGE_SECONDS
    )


def _display_message(response: Dict[str, Any], phase: SyncPhase) -> str:
    if response.get("err"):
        return str(response["err"])
    if response.get("alert") is True and not response.get("status"):
        return "The most recent sync reports alert=true"
    if response.get("status"):
        return str(response["status"])
    labels = {
        SyncPhase.IDLE: "No active sync state reported",
        SyncPhase.REQUESTED: "Sync requested",
        SyncPhase.WAITING: "Waiting for network",
        SyncPhase.CONNECTING: "Connecting",
        SyncPhase.SYNCHRONIZING: "Synchronizing",
        SyncPhase.COMPLETED: "Sync completed",
        SyncPhase.FAILED: "Sync failed",
        SyncPhase.UNRESOLVED: "Sync status unavailable; state unresolved",
        SyncPhase.TIMED_OUT: "Sync still pending at the local timeout",
    }
    return labels[phase]


class SyncMonitor:
    """Initiate at most one directional sync, then poll passive status only."""

    def __init__(
        self,
        client: NotecardClient,
        poll_seconds: float = 10.0,
        timeout_seconds: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.client = client
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self.clock = clock
        self.sleeper = sleeper

    def _emit(
        self,
        updates: List[SyncUpdate],
        started_at: float,
        phase: SyncPhase,
        response: Dict[str, Any],
        callback: Optional[Callable[[SyncUpdate], None]],
        message: Optional[str] = None,
    ) -> SyncUpdate:
        update = SyncUpdate(
            elapsed=max(0, int(self.clock() - started_at)),
            phase=phase,
            message=message or _display_message(response, phase),
            response=response,
        )
        updates.append(update)
        if callback is not None:
            callback(update)
        return update

    @staticmethod
    def _new_completion(
        response: Dict[str, Any],
        baseline: Dict[str, Any],
        observed_cycle: bool,
    ) -> bool:
        response_requested = response.get("requested")
        response_completed = response.get("completed")
        if (
            isinstance(response_requested, (int, float))
            and not isinstance(response_requested, bool)
            and isinstance(response_completed, (int, float))
            and not isinstance(response_completed, bool)
            and response_requested < response_completed
        ):
            # The explicit request is newer than the reported completion, so a
            # terminal-looking status still belongs to the previous cycle.
            return False

        response_time = response.get("time")
        baseline_time = baseline.get("time")
        if (
            isinstance(response_time, int)
            and not isinstance(response_time, bool)
            and isinstance(baseline_time, int)
            and not isinstance(baseline_time, bool)
            and response_time > baseline_time
        ):
            return True
        baseline_completed = baseline.get("completed")
        if (
            isinstance(response_completed, (int, float))
            and isinstance(baseline_completed, (int, float))
            and not isinstance(response_completed, bool)
            and not isinstance(baseline_completed, bool)
            and response_completed < baseline_completed
        ):
            return True

        response_tags = status_tags(response.get("status")) & COMPLETION_TAGS
        baseline_tags = status_tags(baseline.get("status")) & COMPLETION_TAGS
        if response_tags and not (response_tags & baseline_tags):
            return True
        return observed_cycle and bool(response_tags)

    @staticmethod
    def _new_cycle_evidence(
        response: Dict[str, Any], baseline: Dict[str, Any]
    ) -> bool:
        phase = classify_sync_status(response)
        if phase not in {
            SyncPhase.REQUESTED,
            SyncPhase.WAITING,
            SyncPhase.CONNECTING,
            SyncPhase.SYNCHRONIZING,
        }:
            return False

        if str(response.get("status", "")) != str(baseline.get("status", "")):
            return True
        response_requested = response.get("requested")
        baseline_requested = baseline.get("requested")
        if not isinstance(response_requested, (int, float)) or isinstance(
            response_requested, bool
        ):
            return False
        if isinstance(baseline_requested, (int, float)) and not isinstance(
            baseline_requested, bool
        ):
            return response_requested < baseline_requested
        return 0 <= response_requested <= MAX_UNCORRELATED_REQUEST_AGE_SECONDS

    @classmethod
    def _new_failure(
        cls,
        response: Dict[str, Any],
        baseline: Dict[str, Any],
        observed_cycle: bool,
    ) -> bool:
        if observed_cycle or classify_sync_status(baseline) != SyncPhase.FAILED:
            return True
        if response.get("err") != baseline.get("err"):
            return True
        if str(response.get("status", "")) != str(baseline.get("status", "")):
            return True
        return cls._new_cycle_evidence(response, baseline)

    def _sleep_for_poll(self, started_at: float) -> bool:
        remaining = self.timeout_seconds - (self.clock() - started_at)
        if remaining <= 0:
            return False
        self.sleeper(min(self.poll_seconds, remaining))
        return True

    def _poll_unresolved(
        self,
        direction: str,
        requested: bool,
        updates: List[SyncUpdate],
        started_at: float,
        callback: Optional[Callable[[SyncUpdate], None]],
        exc: BaseException,
        last_response: Dict[str, Any],
    ) -> SyncResult:
        response = {
            "last_response": last_response,
            "poll_error": str(exc),
        }
        if requested:
            message = (
                "hub.sync.status became unavailable after this tool sent one "
                "sync request. Its state is unresolved and it may still be "
                "pending; no retry was sent."
            )
        else:
            message = (
                "hub.sync.status became unavailable while monitoring an "
                "existing sync. Its state is unresolved; no duplicate sync "
                "was sent."
            )
        self._emit(
            updates,
            started_at,
            SyncPhase.UNRESOLVED,
            response,
            callback,
            "{} Error: {}".format(message, exc),
        )
        return SyncResult(
            direction=direction,
            phase=SyncPhase.UNRESOLVED,
            requested=requested,
            updates=updates,
            response=response,
            error="{} Error: {} Wait until status is readable before considering another sync.".format(
                message, exc
            ),
        )

    def _monitor_existing(
        self,
        direction: str,
        initial: Dict[str, Any],
        started_at: float,
        updates: List[SyncUpdate],
        callback: Optional[Callable[[SyncUpdate], None]],
    ) -> SyncResult:
        self._emit(
            updates,
            started_at,
            classify_sync_status(initial),
            initial,
            callback,
            "An earlier sync appears active; monitoring it without issuing another request.",
        )
        initial_phase = classify_sync_status(initial)
        observed_cycle = initial_phase in {
            SyncPhase.REQUESTED,
            SyncPhase.WAITING,
            SyncPhase.CONNECTING,
            SyncPhase.SYNCHRONIZING,
        }
        while self.clock() - started_at < self.timeout_seconds:
            if not self._sleep_for_poll(started_at):
                break
            try:
                response = self.client.inspect({"req": "hub.sync.status"})
            except (Exception, KeyboardInterrupt) as exc:
                return self._poll_unresolved(
                    direction,
                    requested=False,
                    updates=updates,
                    started_at=started_at,
                    callback=callback,
                    exc=exc,
                    last_response=updates[-1].response if updates else initial,
                )
            response_error = sync_status_query_error(response)
            if response_error:
                return self._poll_unresolved(
                    direction,
                    requested=False,
                    updates=updates,
                    started_at=started_at,
                    callback=callback,
                    exc=SatphoneError(response_error),
                    last_response=response,
                )
            phase = classify_sync_status(response)
            if self._new_cycle_evidence(response, initial):
                observed_cycle = True
            self._emit(updates, started_at, phase, response, callback)
            if phase == SyncPhase.FAILED:
                return SyncResult(
                    direction=direction,
                    phase=phase,
                    requested=False,
                    updates=updates,
                    response=response,
                    error=str(response.get("err") or response.get("status") or "Sync failed"),
                )
            if phase == SyncPhase.COMPLETED and self._new_completion(
                response, initial, observed_cycle=observed_cycle
            ):
                return SyncResult(
                    direction=direction,
                    phase=SyncPhase.COMPLETED,
                    requested=False,
                    updates=updates,
                    response=response,
                )
            if phase == SyncPhase.IDLE and self._new_completion(
                response, initial, observed_cycle=observed_cycle
            ):
                return SyncResult(
                    direction=direction,
                    phase=SyncPhase.COMPLETED,
                    requested=False,
                    updates=updates,
                    response=response,
                )
        response = updates[-1].response if updates else initial
        self._emit(
            updates,
            started_at,
            SyncPhase.TIMED_OUT,
            response,
            callback,
            "The earlier sync is still unresolved; no duplicate sync was sent.",
        )
        return SyncResult(
            direction=direction,
            phase=SyncPhase.TIMED_OUT,
            requested=False,
            updates=updates,
            response=response,
            error="Existing sync did not reach a terminal state before the local timeout.",
        )

    def run(
        self,
        direction: str,
        callback: Optional[Callable[[SyncUpdate], None]] = None,
    ) -> SyncResult:
        if direction not in ("in", "out"):
            raise ValueError("direction must be 'in' or 'out'")

        updates: List[SyncUpdate] = []
        started_at = self.clock()
        hub_config = self.client.inspect({"req": "hub.get"})
        preflight_error = hub_sync_preflight_error(hub_config)
        if preflight_error:
            message = "{}; no sync request was sent.".format(preflight_error)
            self._emit(
                updates,
                started_at,
                SyncPhase.FAILED,
                hub_config,
                callback,
                message,
            )
            return SyncResult(
                direction=direction,
                phase=SyncPhase.FAILED,
                requested=False,
                updates=updates,
                response=hub_config,
                error=message,
            )

        baseline = self.client.inspect({"req": "hub.sync.status"})
        baseline_error = sync_status_query_error(baseline)
        if baseline_error:
            message = "{}; no sync request was sent.".format(baseline_error)
            self._emit(
                updates,
                started_at,
                SyncPhase.FAILED,
                baseline,
                callback,
                message,
            )
            return SyncResult(
                direction=direction,
                phase=SyncPhase.FAILED,
                requested=False,
                updates=updates,
                response=baseline,
                error=message,
            )
        if sync_status_is_active(baseline):
            return self._monitor_existing(
                direction, baseline, started_at, updates, callback
            )

        request: Dict[str, Any] = {"req": "hub.sync", direction: True}
        try:
            accepted = self.client.inspect(request)
        except (Exception, KeyboardInterrupt) as exc:
            attempted = {"request": request, "error": str(exc)}
            message = (
                "The sync request exhausted the library's replay-safe transaction "
                "handling and may have reached the Notecard; no application-level "
                "retry was sent: {}".format(exc)
            )
            self._emit(
                updates,
                started_at,
                SyncPhase.UNRESOLVED,
                attempted,
                callback,
                message,
            )
            return SyncResult(
                direction=direction,
                phase=SyncPhase.UNRESOLVED,
                requested=True,
                updates=updates,
                response=attempted,
                error="{} Wait until hub.sync.status is readable before considering another sync.".format(
                    message
                ),
            )

        accepted_phase = classify_sync_status(accepted)
        if accepted_phase == SyncPhase.FAILED:
            self._emit(
                updates,
                started_at,
                SyncPhase.FAILED,
                accepted,
                callback,
            )
            return SyncResult(
                direction=direction,
                phase=SyncPhase.FAILED,
                requested=True,
                updates=updates,
                response=accepted,
                error=str(
                    accepted.get("err")
                    or accepted.get("status")
                    or "Sync request failed"
                ),
            )

        if accepted_phase == SyncPhase.COMPLETED:
            self._emit(
                updates,
                started_at,
                SyncPhase.COMPLETED,
                accepted,
                callback,
            )
            return SyncResult(
                direction=direction,
                phase=SyncPhase.COMPLETED,
                requested=True,
                updates=updates,
                response=accepted,
            )

        self._emit(
            updates,
            started_at,
            SyncPhase.REQUESTED,
            accepted,
            callback,
            "{}bound sync request accepted".format(
                "In" if direction == "in" else "Out"
            ),
        )

        observed_cycle = False
        last_response = accepted
        while self.clock() - started_at < self.timeout_seconds:
            if not self._sleep_for_poll(started_at):
                break
            try:
                response = self.client.inspect({"req": "hub.sync.status"})
            except (Exception, KeyboardInterrupt) as exc:
                return self._poll_unresolved(
                    direction,
                    requested=True,
                    updates=updates,
                    started_at=started_at,
                    callback=callback,
                    exc=exc,
                    last_response=last_response,
                )
            last_response = response
            response_error = sync_status_query_error(response)
            if response_error:
                return self._poll_unresolved(
                    direction,
                    requested=True,
                    updates=updates,
                    started_at=started_at,
                    callback=callback,
                    exc=SatphoneError(response_error),
                    last_response=response,
                )
            phase = classify_sync_status(response)
            if self._new_cycle_evidence(response, baseline):
                observed_cycle = True

            if phase == SyncPhase.COMPLETED:
                if self._new_completion(response, baseline, observed_cycle):
                    self._emit(updates, started_at, phase, response, callback)
                    return SyncResult(
                        direction=direction,
                        phase=phase,
                        requested=True,
                        updates=updates,
                        response=response,
                    )
                # This is the previous terminal status; do not report false success.
                phase = SyncPhase.REQUESTED
            elif phase == SyncPhase.IDLE and self._new_completion(
                response, baseline, observed_cycle
            ):
                phase = SyncPhase.COMPLETED
                self._emit(updates, started_at, phase, response, callback)
                return SyncResult(
                    direction=direction,
                    phase=phase,
                    requested=True,
                    updates=updates,
                    response=response,
                )

            if phase == SyncPhase.FAILED:
                if self._new_failure(response, baseline, observed_cycle):
                    self._emit(updates, started_at, phase, response, callback)
                    return SyncResult(
                        direction=direction,
                        phase=phase,
                        requested=True,
                        updates=updates,
                        response=response,
                        error=str(
                            response.get("err")
                            or response.get("status")
                            or "Sync failed"
                        ),
                    )
                phase = SyncPhase.REQUESTED
                self._emit(
                    updates,
                    started_at,
                    phase,
                    response,
                    callback,
                    "The previous sync failure is still reported; awaiting evidence from the new request.",
                )
                continue

            self._emit(updates, started_at, phase, response, callback)

        self._emit(
            updates,
            started_at,
            SyncPhase.TIMED_OUT,
            last_response,
            callback,
            "Local timeout reached. This is not proof of satellite delivery failure; the request may remain queued.",
        )
        return SyncResult(
            direction=direction,
            phase=SyncPhase.TIMED_OUT,
            requested=True,
            updates=updates,
            response=last_response,
            error="Sync did not complete before the local timeout.",
        )
