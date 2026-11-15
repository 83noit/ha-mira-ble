"""Diagnostics download: entry data, Bluetooth paths, recent frames and poll statistics."""

from __future__ import annotations

from dataclasses import asdict
import re
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_PAIRED_SOURCE,
    CONF_PRESETS,
    CONF_SERIAL_NUMBER,
    CONF_VALVE_NAME,
)
from .coordinator import MiraConfigEntry

TO_REDACT = {CONF_SERIAL_NUMBER, CONF_VALVE_NAME}
_ADDRESS = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")


def redact_address(address: str) -> str:
    """Keep the last two bytes of a Bluetooth address, enough to tell devices apart."""
    parts = address.split(":")
    if len(parts) != 6:
        return REDACTED
    return "**:**:**:**:" + ":".join(parts[4:])


def _scrub(text: str | None, entry: MiraConfigEntry) -> str | None:
    """Redact Bluetooth addresses, the valve's names and its serial number in free text."""
    if text is None:
        return None
    scrubbed = _ADDRESS.sub(lambda match: redact_address(match.group(0)), text)
    secrets = [entry.title, entry.data.get(CONF_VALVE_NAME), entry.data.get(CONF_SERIAL_NUMBER)]
    secrets += [preset.get("name") for preset in entry.data.get(CONF_PRESETS, ())]
    for secret in secrets:
        if secret:
            scrubbed = scrubbed.replace(str(secret), REDACTED)
    return scrubbed


def _proxy_firmware(hass: HomeAssistant, source: str) -> str | None:
    """Return the software version of the device behind a scanner, when Home Assistant knows it.

    For an ESPHome proxy this is its ESPHome version, which is what identifies firmware that
    forgets bonds (ESPHome 2026.10 builds on ESP-IDF 5.5.6).
    """
    wanted = dr.format_mac(source)
    for device in dr.async_get(hass).devices:
        macs = {dr.format_mac(value) for _kind, value in device.connections}
        if wanted in macs and device.sw_version:
            return str(device.sw_version)
    return None


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MiraConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    address: str = entry.data[CONF_ADDRESS]
    entry_data = async_redact_data(dict(entry.data), TO_REDACT)
    entry_data[CONF_ADDRESS] = redact_address(address)
    entry_data[CONF_PAIRED_SOURCE] = _scrub(entry.data.get(CONF_PAIRED_SOURCE), entry)
    # Preset names are chosen by people; the slots are enough to debug with.
    entry_data[CONF_PRESETS] = [
        {**preset, "name": REDACTED} for preset in entry.data.get(CONF_PRESETS, ())
    ]
    data = coordinator.data
    return {
        "entry": {"data": entry_data, "unique_id_is_serial": entry.unique_id != address},
        "bluetooth": {
            "scanners_hearing_the_valve": [
                {
                    "source": redact_address(device.scanner.source),
                    "connectable": device.scanner.connectable,
                    "rssi": device.advertisement.rssi,
                    "firmware": _proxy_firmware(hass, device.scanner.source),
                }
                for device in bluetooth.async_scanner_devices_by_address(
                    hass, address, connectable=False
                )
            ],
            "last_connection_via": _scrub(coordinator.last_source, entry),
            "reachability": _scrub(coordinator.reachability(), entry),
        },
        "polling": {
            "interval": coordinator.update_interval.total_seconds()
            if coordinator.update_interval
            else None,
            "poll_count": coordinator.poll_count,
            "last_update_success": coordinator.last_update_success,
            "consecutive_failures": coordinator.consecutive_failures,
            "pairing_refusals": coordinator.pairing_refusals,
            "reauth_raised": coordinator.reauth_raised,
            "last_error": _scrub(coordinator.last_error, entry),
            "optional_reads_backing_off": dict(coordinator.optional_skips),
            "presets_verifiable": coordinator.presets_verifiable,
        },
        "outlets": {number: outlet.as_dict() for number, outlet in coordinator.outlets.items()},
        "setpoints": {
            "temperature": coordinator.temperature_setpoint,
            "flow": coordinator.flow_setpoint,
        },
        "state": {
            **asdict(data.state),
            "raw": data.state.raw.hex(" "),
            "outlets": int(data.state.outlets),
            "active": data.state.active,
        },
        "last_usage": asdict(data.last_usage) if data.last_usage else None,
        "last_frames": [
            {"direction": direction, "frame": frame} for direction, frame in coordinator.last_frames
        ],
    }
