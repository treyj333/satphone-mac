"""Tacthrift Notecard Satphone development and diagnostic tool."""

__version__ = "1.2.1"

INBOUND_FILE = "messages.qi"
OUTBOUND_FILE = "messages.qo"
INBOUND_PORT = 56
# Port 55 is already assigned to the existing sat.qo template on this device.
# Notecard compact-template ports must be unique, so messages.qo uses 57.
OUTBOUND_PORT = 57
MESSAGE_TEMPLATE_BODY = {"msg": "-"}
MAX_MESSAGE_BYTES = 160
