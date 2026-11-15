"""Diagnostics carry what a bug report needs and nothing that identifies the household."""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

from custom_components.mira_ble.diagnostics import (
    async_get_config_entry_diagnostics,
    redact_address,
)
from mira_ble.testing import FakeValve

from .conftest import PROXY_SOURCE, SERIAL, VALVE_ADDRESS


async def test_diagnostics(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    coordinator = setup_entry.runtime_data
    coordinator.last_error = f"RequestTimeout: Bathroom: no reply from {VALVE_ADDRESS} ({SERIAL})"
    diagnostics = await async_get_config_entry_diagnostics(hass, setup_entry)
    text = json.dumps(diagnostics)
    assert VALVE_ADDRESS not in text
    assert PROXY_SOURCE not in text
    assert SERIAL not in text
    assert "Bathroom" not in text
    assert diagnostics["entry"]["data"]["address"] == "**:**:**:**:12:34"
    assert diagnostics["entry"]["data"]["paired_source"] == "**:**:**:**:AA:01"
    assert diagnostics["entry"]["unique_id_is_serial"] is True
    assert diagnostics["entry"]["data"]["outlets"][1]["kind"] == "overhead"
    assert diagnostics["bluetooth"]["scanners_hearing_the_valve"] == [
        {"source": "**:**:**:**:AA:01", "connectable": True, "rssi": -67, "firmware": None}
    ]
    assert diagnostics["polling"]["poll_count"] == 1
    assert diagnostics["polling"]["interval"] == 60
    assert "**:**:**:**:12:34" in diagnostics["polling"]["last_error"]
    assert diagnostics["state"]["water_temperature"] == 25.6
    assert diagnostics["state"]["raw"].startswith("21 20")
    assert diagnostics["last_usage"] == {"duration": 10, "volume": 1, "temperature": 44.0}
    assert diagnostics["last_frames"][0] == {"direction": "tx", "frame": "aa 55 00 2b 01 02 d3"}


async def test_diagnostics_without_an_error_or_a_known_pairing_path(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    hass.config_entries.async_update_entry(
        setup_entry, data={k: v for k, v in setup_entry.data.items() if k != "paired_source"}
    )
    diagnostics = await async_get_config_entry_diagnostics(hass, setup_entry)
    assert diagnostics["polling"]["last_error"] is None
    assert diagnostics["entry"]["data"]["paired_source"] is None


def test_redact_address() -> None:
    assert redact_address("AA:BB:CC:DD:12:34") == "**:**:**:**:12:34"
    assert redact_address("12345678-1234-5678-1234-56789ABCDEF0") == "**REDACTED**"
