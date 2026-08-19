"""Location inspection and explicitly-confirmed fixed test configuration."""

import math
from dataclasses import replace
from typing import Any, Dict, Optional

from .models import LocationInfo
from .notecard import NotecardClient, NotecardRequestError, SatphoneError


LOCATION_MODES = {"continuous", "periodic", "off", "fixed"}


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def inspect_location(client: NotecardClient) -> LocationInfo:
    response = client.inspect({"req": "card.location"})
    latitude = response.get("lat")
    longitude = response.get("lon")
    known = (
        not response.get("err")
        and _number(latitude)
        and _number(longitude)
        and -90 <= float(latitude) <= 90
        and -180 <= float(longitude) <= 180
    )
    status = str(response.get("status", ""))
    mode = response.get("mode")
    lowered = status.lower()
    if "{gps}" in lowered:
        source = "GPS/GNSS"
    elif known:
        source = "last-known or triangulated"
    else:
        source = "unavailable"

    return LocationInfo(
        known=known,
        latitude=float(latitude) if known else None,
        longitude=float(longitude) if known else None,
        captured_at=response.get("time") if isinstance(response.get("time"), int) else None,
        mode=str(mode) if mode is not None else None,
        status=status,
        source=source,
        dop=float(response["dop"]) if _number(response.get("dop")) else None,
        failure_count=response.get("count") if isinstance(response.get("count"), int) else None,
        raw=response,
    )


def location_configuration(client: NotecardClient) -> Dict[str, Dict[str, Any]]:
    return {
        "card.location.mode": client.inspect({"req": "card.location.mode"}),
        "ntn.gps": client.inspect({"req": "ntn.gps"}),
    }


def apply_location_configuration(
    location: LocationInfo,
    configuration: Dict[str, Dict[str, Any]],
) -> LocationInfo:
    """Add fixed-source evidence from card.location.mode's separate response."""
    mode_response = configuration.get("card.location.mode") or {}
    if location.known and mode_response.get("mode") == "fixed":
        return replace(location, mode="fixed", source="fixed")
    return location


def location_configuration_error(
    configuration: Dict[str, Dict[str, Any]]
) -> Optional[str]:
    """Return why a configuration snapshot is unsafe to mutate from."""
    mode = configuration.get("card.location.mode") or {}
    gps = configuration.get("ntn.gps") or {}
    if mode.get("err") or mode.get("_exception"):
        return "card.location.mode failed: {}".format(
            mode.get("err") or mode.get("_exception")
        )
    if gps.get("err") or gps.get("_exception"):
        return "ntn.gps failed: {}".format(
            gps.get("err") or gps.get("_exception")
        )
    if not isinstance(mode.get("mode"), str) or not mode.get("mode"):
        return "card.location.mode did not return a mode"
    if mode.get("mode") not in LOCATION_MODES:
        return "card.location.mode returned an unrecognized mode: {}".format(
            mode.get("mode")
        )
    gps_on = gps.get("on") is True
    gps_off = gps.get("off") is True
    if gps_on == gps_off:
        return "ntn.gps did not return exactly one active on/off state"
    return None


def set_fixed_test_location(
    client: NotecardClient, location: LocationInfo
) -> Dict[str, Dict[str, Any]]:
    """Apply a fixed location after the CLI has displayed state and confirmed."""
    if not location.known or location.latitude is None or location.longitude is None:
        raise ValueError("A real last-known location is required; coordinates were not changed.")
    if location.source not in ("GPS/GNSS", "fixed"):
        raise ValueError(
            "Only GPS/GNSS or an existing fixed location may be reused; "
            "tower or triangulated coordinates were not applied."
        )
    current = location_configuration(client)
    current_error = location_configuration_error(current)
    if current_error:
        raise SatphoneError(
            "Current location configuration could not be verified, so no change "
            "was made: {}".format(current_error)
        )
    try:
        mode_response = client.request(
            {
                "req": "card.location.mode",
                "mode": "fixed",
                "lat": location.latitude,
                "lon": location.longitude,
            }
        )
    except NotecardRequestError as exc:
        raise SatphoneError(
            "The Notecard rejected the fixed-location write; ntn.gps was not "
            "changed: {}".format(exc)
        ) from exc
    except (Exception, KeyboardInterrupt) as exc:
        raise SatphoneError(
            "The fixed-location write did not return a final response and may "
            "have applied. ntn.gps was not changed; inspect both settings "
            "before retrying: {}".format(exc)
        ) from exc
    try:
        gps_response = client.request({"req": "ntn.gps", "on": True})
    except (Exception, KeyboardInterrupt) as exc:
        raise SatphoneError(
            "The fixed Notecard location was applied, but ntn.gps on=true failed. "
            "The device is partially configured; inspect both settings before retrying: {}".format(
                exc
            )
        ) from exc
    try:
        verified = location_configuration(client)
        verified_location = inspect_location(client)
    except (Exception, KeyboardInterrupt) as exc:
        raise SatphoneError(
            "Both location writes returned, but their persistent state could not be "
            "verified. Inspect card.location.mode and ntn.gps before retrying: {}".format(
                exc
            )
        ) from exc
    verification_error = location_configuration_error(verified)
    coordinates_match = (
        verified_location.known
        and verified_location.latitude is not None
        and verified_location.longitude is not None
        and math.isclose(
            verified_location.latitude,
            location.latitude,
            rel_tol=0.0,
            abs_tol=1e-6,
        )
        and math.isclose(
            verified_location.longitude,
            location.longitude,
            rel_tol=0.0,
            abs_tol=1e-6,
        )
    )
    if (
        verification_error
        or verified["card.location.mode"].get("mode") != "fixed"
        or verified["ntn.gps"].get("on") is not True
        or not coordinates_match
    ):
        raise SatphoneError(
            "Location writes returned, but the required fixed/on state was not "
            "verified with the requested coordinates. Inspect both settings before "
            "retrying: {}; card.location={} ({})".format(
                verified,
                verified_location.raw,
                verification_error or "unexpected resulting state",
            )
        )
    return {
        "card.location.mode": mode_response,
        "ntn.gps": gps_response,
        "verified": verified,
        "verified_location": verified_location.raw,
    }
