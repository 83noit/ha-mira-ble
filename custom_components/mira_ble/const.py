"""Constants for the Mira Bluetooth integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "mira_ble"
MANUFACTURER: Final = "Kohler Mira"

CONF_SERIAL_NUMBER: Final = "serial_number"
CONF_PRODUCT_CODE: Final = "product_code"
CONF_VALVE_NAME: Final = "valve_name"
CONF_OUTLETS: Final = "outlets"
CONF_PRESETS: Final = "presets"
CONF_PRESETS_UNREADABLE: Final = "presets_unreadable"
"""True when the valve did not answer preset reads at pairing (a Platinum at rest)."""
CONF_READ_ERRORS: Final = "read_errors"
CONF_PAIRED_SOURCE: Final = "paired_source"
CONF_PAIRED_AT: Final = "paired_at"

SERVICE_START: Final = "start"
SERVICE_RUN_PRESET: Final = "run_preset"
ATTR_TEMPERATURE: Final = "temperature"
ATTR_FLOW: Final = "flow"

IDLE_INTERVAL: Final = 60
"""Seconds between polls while nothing runs; each is one short connection."""
RUNNING_INTERVAL: Final = 5
"""Seconds between polls while water runs; the link is held open for the run."""
REAUTH_INTERVAL: Final = 600
"""Seconds between polls while re-pairing is outstanding, so a recovery is still noticed."""
RUNNING_RETRIES: Final = 6
"""Failed polls after an active reading that are still retried at the running rate."""
FAILURES_BEFORE_UNAVAILABLE: Final = 3
"""Consecutive failed polls before readings go unavailable; fewer are noise on a radio link."""
OUTLET_REFRESH: Final = 1800
"""Seconds between re-reads of the outlets' limits, which can be changed in the vendor app."""
MAX_OPTIONAL_BACKOFF: Final = 32
"""Most polls an optional read that went unanswered is skipped before it is tried again."""

POLL_TIMEOUT: Final = 60.0
"""Upper bound on one poll, connection attempts included."""
COMMAND_TIMEOUT: Final = 90.0
"""Upper bound on one command: two connection attempts, a read, the command, its confirmation
and, for a start that could not be confirmed, a stop."""
PAIR_TIMEOUT: Final = 120.0
"""Upper bound on the pairing step: connecting, pairing and up to a dozen reads, each of which
has been reported to take several seconds on a slow link."""
THROUGH_TIMEOUT: Final = 300.0
"""Seconds a preset started with "run preset" may spend warming up before the integration
stops waiting to start the preset proper."""
STOP_ATTEMPTS: Final = 3
"""Times a stop is tried, each on a fresh connection, before it is reported as failed."""

MAX_PAIRING_REFUSALS: Final = 3
"""Consecutive polls refused outright, through the bonded adapter, before re-pairing is asked."""
BOND_GRACE: Final = 3600
"""Seconds after a successful pairing in which a refusal points at the proxy, not the bond.

ESP-IDF 5.5.6 (ESPHome 2026.10) forgets a bond after the first session until the proxy
restarts; pairing again does not help.
"""
OUTAGE_ISSUE_AFTER: Final = 3600
"""Seconds of failed polls, while the valve is still heard, before a repair issue is raised."""

FALLBACK_MIN_TEMPERATURE: Final = 30.0
FALLBACK_MAX_TEMPERATURE: Final = 45.0
FALLBACK_TEMPERATURE: Final = 38.0
FALLBACK_MIN_FLOW: Final = 4.0
FALLBACK_MAX_FLOW: Final = 16.0
FALLBACK_FLOW: Final = 8.0
"""Limits used for a valve whose outlet configuration could not be read."""
UNVERIFIED_PRESET_SLOTS: Final = (1, 2, 3)
"""Slots offered, disabled by default, when the valve does not answer preset reads."""
