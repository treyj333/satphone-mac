"""Read-only diagnostic checks and specific remediation guidance."""

import json
from datetime import datetime
from typing import Any, Dict, Tuple

from .location import (
    apply_location_configuration,
    inspect_location,
    location_configuration_error,
)
from .messages import INBOUND_TEMPLATE, OUTBOUND_TEMPLATE, inbox_count, verify_template
from .models import CheckLevel, DiagnosticCheck, DiagnosticSnapshot, TemplateCheck
from .notecard import NotecardClient, has_error_tag, notecard_identity_error
from .satellite import (
    FAILURE_TAGS,
    classify_sync_status,
    hub_sync_preflight_error,
    status_tags,
    sync_status_query_error,
    sync_status_is_active,
    transport_supports_ntn,
)


class DiagnosticRunner:
    def __init__(self, client: NotecardClient):
        self.client = client

    def _query(
        self, raw: Dict[str, Any], label: str, request: Dict[str, Any]
    ) -> Dict[str, Any]:
        try:
            response = self.client.inspect(request)
        except Exception as exc:
            response = {"_exception": str(exc)}
        raw[label] = response
        return response

    @staticmethod
    def _request_failure(
        name: str, response: Dict[str, Any], action: str
    ) -> DiagnosticCheck:
        error = response.get("err") or response.get("_exception") or "Unknown API error"
        return DiagnosticCheck(
            name=name,
            level=CheckLevel.FAIL,
            summary=str(error),
            recommendation=action,
        )

    @staticmethod
    def _template_check(check: TemplateCheck) -> DiagnosticCheck:
        if check.query_error and not check.repairable:
            return DiagnosticCheck(
                name="{} template".format(check.spec.file),
                level=CheckLevel.FAIL,
                summary="Template could not be verified: {}".format(check.query_error),
                recommendation="Resolve the read/query error first. Do not overwrite a template whose current state is unknown.",
            )
        if check.valid:
            return DiagnosticCheck(
                name="{} template".format(check.spec.file),
                level=CheckLevel.PASS,
                summary="Compact NTN template is valid (port {}).".format(
                    check.spec.port
                ),
            )
        if not check.exists:
            return DiagnosticCheck(
                name="{} template".format(check.spec.file),
                level=CheckLevel.FAIL,
                summary="Template is missing or could not be verified.",
                detail=str(check.response.get("err", "No active template returned.")),
                recommendation="Review the proposed template, approve its creation, then sync it to Notehub over cellular or Wi-Fi before using NTN.",
            )
        return DiagnosticCheck(
            name="{} template".format(check.spec.file),
            level=CheckLevel.FAIL,
            summary="Template differs from the expected message schema.",
            detail=json.dumps(check.differences, sort_keys=True),
            recommendation="Review the field-by-field difference before approving a repair. A repaired template must be synced terrestrially once.",
        )

    def run(self) -> Tuple[DiagnosticSnapshot, Dict[str, TemplateCheck]]:
        raw: Dict[str, Any] = {}
        checks = []

        version = self._query(raw, "card.version", {"req": "card.version"})
        version_error = notecard_identity_error(version)
        if version.get("_exception") or version_error:
            checks.append(
                DiagnosticCheck(
                    name="Notecard",
                    level=CheckLevel.FAIL,
                    summary=str(version.get("_exception") or version_error),
                    recommendation="Reconnect USB, close other serial terminals, and retry.",
                )
            )
        else:
            identity = version.get("name") or version.get("version")
            checks.append(
                DiagnosticCheck(
                    name="Notecard",
                    level=CheckLevel.PASS,
                    summary="Connected and responding{}.".format(
                        " ({})".format(identity) if identity else ""
                    ),
                )
            )

        ntn = self._query(raw, "ntn.status", {"req": "ntn.status"})
        ntn_text = "{} {}".format(ntn.get("status", ""), ntn.get("err", ""))
        ntn_tags = status_tags(ntn_text)
        if has_error_tag(ntn_text, "no-ntn-module"):
            checks.append(
                DiagnosticCheck(
                    name="StarNote",
                    level=CheckLevel.FAIL,
                    summary="No NTN module is connected.",
                    recommendation="Check StarNote power and its UART connection to the Notecard. Do not run ntn.reset as a discovery step.",
                )
            )
        elif ntn.get("err") or ntn.get("_exception"):
            checks.append(
                self._request_failure(
                    "StarNote",
                    ntn,
                    "Inspect the exact NTN error and physical connection before changing configuration.",
                )
            )
        elif "ntn-idle" in ntn_tags:
            checks.append(
                DiagnosticCheck(
                    name="StarNote",
                    level=CheckLevel.PASS,
                    summary="Responding and idle (the documented ready state after pairing).",
                    detail="Blues does not expose a separate paired boolean; this is inferred from ntn.status.",
                )
            )
        elif "ntn-connect-failure" in ntn_tags:
            checks.append(
                DiagnosticCheck(
                    name="StarNote",
                    level=CheckLevel.FAIL,
                    summary="StarNote could not establish a satellite connection.",
                    detail=str(ntn.get("status", "")),
                    recommendation="Move to a clear sky view and inspect location/network status before retrying.",
                )
            )
        elif ntn_tags & {
            "ntn-initializing",
            "ntn-power",
            "ntn-connecting",
            "ntn-connected",
            "ntn-disconnecting",
            "ntn-uplinking",
            "ntn-downlinking",
            "ntn-enabling-gps",
            "ntn-disabling-gps",
        }:
            checks.append(
                DiagnosticCheck(
                    name="StarNote",
                    level=CheckLevel.INFO,
                    summary="Detected and currently active: {}".format(
                        ntn.get("status", "unknown state")
                    ),
                )
            )
        else:
            checks.append(
                DiagnosticCheck(
                    name="StarNote",
                    level=CheckLevel.WARN,
                    summary="NTN response is not a recognized ready state.",
                    detail=str(ntn),
                    recommendation="Use raw debug mode if this state persists; do not infer pairing from transport configuration alone.",
                )
            )

        if "ntn-unknown-location" in ntn_tags and not ntn.get("err"):
            checks.append(
                DiagnosticCheck(
                    name="StarNote location",
                    level=CheckLevel.WARN,
                    summary="StarNote reports that its satellite location is unknown.",
                    recommendation="Obtain a clear-sky GNSS fix or explicitly approve a real fixed Notecard location plus ntn.gps on=true.",
                )
            )

        transport = self._query(raw, "card.transport", {"req": "card.transport"})
        if transport.get("err") or transport.get("_exception"):
            checks.append(
                self._request_failure(
                    "Transport",
                    transport,
                    "Verify firmware support and inspect card.transport manually.",
                )
            )
        elif transport_supports_ntn(transport):
            checks.append(
                DiagnosticCheck(
                    name="Transport",
                    level=CheckLevel.PASS,
                    summary="NTN-capable method configured: {}.".format(
                        transport.get("method")
                    ),
                    detail="Configuration alone does not prove a real satellite delivery path.",
                )
            )
        else:
            checks.append(
                DiagnosticCheck(
                    name="Transport",
                    level=CheckLevel.FAIL,
                    summary="Current method does not include NTN: {}.".format(
                        transport.get("method", "unknown")
                    ),
                    recommendation="Review the current configuration, then explicitly approve method=ntn if a satellite-only test is intended.",
                )
            )

        try:
            location = inspect_location(self.client)
            raw["card.location"] = location.raw
        except Exception as exc:
            location = None
            raw["card.location"] = {"_exception": str(exc)}
        location_mode = self._query(
            raw, "card.location.mode", {"req": "card.location.mode"}
        )
        gps_override = self._query(raw, "ntn.gps", {"req": "ntn.gps"})
        location_config_error = location_configuration_error(
            {
                "card.location.mode": location_mode,
                "ntn.gps": gps_override,
            }
        )
        if location is not None and not location_config_error:
            location = apply_location_configuration(
                location,
                {
                    "card.location.mode": location_mode,
                    "ntn.gps": gps_override,
                },
            )
        if location_config_error:
            checks.append(
                DiagnosticCheck(
                    name="Location configuration",
                    level=CheckLevel.FAIL,
                    summary=location_config_error,
                    recommendation="Resolve the read error before considering any persistent location change.",
                )
            )
        if location is not None and location.known:
            level = (
                CheckLevel.PASS
                if location.source in ("GPS/GNSS", "fixed")
                else CheckLevel.WARN
            )
            checks.append(
                DiagnosticCheck(
                    name="Location",
                    level=level,
                    summary="Known at {:.6f}, {:.6f} ({}).".format(
                        location.latitude, location.longitude, location.source
                    ),
                    detail="DOP: {}; captured: {}; mode: {}".format(
                        location.dop if location.dop is not None else "not reported",
                        location.captured_at if location.captured_at is not None else "not reported",
                        location.mode or location_mode.get("mode", "unknown"),
                    ),
                    recommendation=(
                        "This may be tower/triangulated rather than a current GNSS fix; inspect status and age before fixing it."
                        if level == CheckLevel.WARN
                        else ""
                    ),
                )
            )
            if (
                not location_config_error
                and location_mode.get("mode") == "fixed"
                and not gps_override.get("on")
            ):
                checks.append(
                    DiagnosticCheck(
                        name="StarNote location source",
                        level=CheckLevel.WARN,
                        summary="Notecard is fixed, but StarNote is not configured to use the Notecard location.",
                        recommendation="After reviewing current state, approve ntn.gps on=true for fixed Skylo testing.",
                    )
                )
        elif location is not None:
            checks.append(
                DiagnosticCheck(
                    name="Location",
                    level=CheckLevel.FAIL,
                    summary="No usable latitude/longitude is available.",
                    detail=location.status or str(location.raw),
                    recommendation="Move outside with a clear sky view and wait for a real fix. Do not enter invented coordinates.",
                )
            )
        else:
            checks.append(
                DiagnosticCheck(
                    name="Location",
                    level=CheckLevel.FAIL,
                    summary="Location request failed: {}".format(
                        raw["card.location"].get("_exception")
                    ),
                    recommendation="Confirm the Notecard remains connected, then retry the read-only diagnostic.",
                )
            )

        template_checks: Dict[str, TemplateCheck] = {}
        for spec in (INBOUND_TEMPLATE, OUTBOUND_TEMPLATE):
            try:
                template_check = verify_template(self.client, spec)
            except Exception as exc:
                template_check = TemplateCheck(
                    spec=spec,
                    exists=False,
                    valid=False,
                    response={"_exception": str(exc)},
                    query_error=str(exc),
                    repairable=False,
                )
            template_checks[spec.file] = template_check
            raw["note.template:{}".format(spec.file)] = template_check.response
            checks.append(self._template_check(template_check))

        hub_config = self._query(raw, "hub.get", {"req": "hub.get"})
        if hub_config.get("err") or hub_config.get("_exception"):
            checks.append(
                self._request_failure(
                    "Notehub configuration",
                    hub_config,
                    "Verify the ProductUID without clearing any existing hub configuration.",
                )
            )
        elif hub_sync_preflight_error(hub_config):
            checks.append(
                DiagnosticCheck(
                    name="Notehub configuration",
                    level=CheckLevel.FAIL,
                    summary=hub_sync_preflight_error(hub_config) or "Invalid Notehub configuration.",
                    recommendation="Review the existing ProductUID and mode before testing sync; do not clear the current configuration.",
                )
            )
        else:
            checks.append(
                DiagnosticCheck(
                    name="Notehub configuration",
                    level=CheckLevel.PASS,
                    summary="ProductUID is configured.",
                    detail="Mode: {}".format(hub_config.get("mode", "not reported")),
                )
            )

        hub = self._query(raw, "hub.status", {"req": "hub.status"})
        if hub.get("err") or hub.get("_exception"):
            checks.append(
                self._request_failure(
                    "Notehub connection",
                    hub,
                    "Inspect the structured hub error and network conditions.",
                )
            )
        elif hub.get("connected"):
            checks.append(
                DiagnosticCheck(
                    name="Notehub connection",
                    level=CheckLevel.PASS,
                    summary="Currently connected to Notehub.",
                )
            )
        else:
            checks.append(
                DiagnosticCheck(
                    name="Notehub connection",
                    level=CheckLevel.INFO,
                    summary="Not currently connected (normal between syncs in minimum/periodic modes).",
                    detail=str(hub.get("status", "")),
                )
            )

        sync = self._query(raw, "hub.sync.status", {"req": "hub.sync.status"})
        sync_tags = status_tags("{} {}".format(sync.get("status", ""), sync.get("err", "")))
        if sync.get("err") or sync.get("_exception"):
            checks.append(
                self._request_failure(
                    "Last sync",
                    sync,
                    "Resolve the specific status code before initiating another satellite attempt.",
                )
            )
        elif sync_status_query_error(sync):
            checks.append(
                DiagnosticCheck(
                    name="Last sync",
                    level=CheckLevel.FAIL,
                    summary=sync_status_query_error(sync) or "Malformed sync status.",
                    recommendation="Treat this response as unknown and do not initiate another satellite attempt until hub.sync.status is readable.",
                )
            )
        elif sync_tags & FAILURE_TAGS:
            checks.append(
                DiagnosticCheck(
                    name="Last sync",
                    level=CheckLevel.FAIL,
                    summary=str(sync.get("status") or "Sync status contains a failure code."),
                    recommendation="Resolve the specific status code before initiating another satellite attempt.",
                )
            )
        elif sync_status_is_active(sync):
            checks.append(
                DiagnosticCheck(
                    name="Last sync",
                    level=CheckLevel.INFO,
                    summary="A sync appears pending or active; no new sync was initiated.",
                    detail="{}{}".format(
                        str(sync.get("status", "")),
                        " (alert=true describes an error in the most recent sync)"
                        if sync.get("alert") is True
                        else "",
                    ),
                )
            )
        elif sync.get("alert") is True:
            checks.append(
                DiagnosticCheck(
                    name="Last sync",
                    level=CheckLevel.FAIL,
                    summary="The most recent sync reports alert=true: {}".format(
                        sync.get("status", "error details not reported")
                    ),
                    recommendation="Inspect the status codes and resolve the reported sync error before retrying.",
                )
            )
        elif (
            (
                isinstance(sync.get("time"), int)
                and not isinstance(sync.get("time"), bool)
            )
            or (
                isinstance(sync.get("completed"), int)
                and not isinstance(sync.get("completed"), bool)
            )
        ) and sync.get("sync") is True:
            checks.append(
                DiagnosticCheck(
                    name="Last sync",
                    level=CheckLevel.WARN,
                    summary="A previous sync completed, but unsynchronized work remains.",
                    detail="Completion age: {} seconds; status: {}".format(
                        sync.get("completed", "not reported"),
                        sync.get("status", "not reported"),
                    ),
                    recommendation="Inspect pending files and avoid blind retries; request the needed direction once.",
                )
            )
        elif (
            isinstance(sync.get("time"), int)
            and not isinstance(sync.get("time"), bool)
        ) or (
            isinstance(sync.get("completed"), int)
            and not isinstance(sync.get("completed"), bool)
        ):
            checks.append(
                DiagnosticCheck(
                    name="Last sync",
                    level=CheckLevel.PASS,
                    summary="A previous sync completed.",
                    detail="Completion age: {} seconds; epoch: {}".format(
                        sync.get("completed", "not reported"),
                        sync.get("time", "not reported"),
                    ),
                )
            )
        else:
            checks.append(
                DiagnosticCheck(
                    name="Last sync",
                    level=CheckLevel.WARN,
                    summary="No completed Notehub sync is recorded.",
                    recommendation="Complete a terrestrial sync first so the StarNote association and templates reach Notehub.",
                )
            )

        try:
            count = inbox_count(self.client)
            raw["messages.qi.count"] = {"total": count}
            checks.append(
                DiagnosticCheck(
                    name="Incoming messages",
                    level=CheckLevel.INFO,
                    summary=(
                        "No local incoming messages."
                        if count == 0
                        else "{} local incoming message(s), left unread and undeleted.".format(count)
                    ),
                )
            )
        except Exception as exc:
            raw["messages.qi.count"] = {"_exception": str(exc)}
            checks.append(
                DiagnosticCheck(
                    name="Incoming messages",
                    level=CheckLevel.WARN,
                    summary="Inbox count could not be read: {}".format(exc),
                )
            )

        return (
            DiagnosticSnapshot(
                timestamp=datetime.now().astimezone(),
                serial_port=self.client.device,
                checks=checks,
                raw=raw,
            ),
            template_checks,
        )
