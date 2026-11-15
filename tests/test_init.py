"""Setup, unload, and the first poll."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.mira_ble.const import DOMAIN
from mira_ble.testing import FakeValve

from .conftest import SERIAL, VALVE_ADDRESS, FakeProxyScanner


async def test_setup_creates_the_device_and_entities(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    assert setup_entry.state is ConfigEntryState.LOADED
    assert not valve.is_connected  # idle: the link is dropped after the poll
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, SERIAL), config_entry_id=setup_entry.entry_id
    )
    assert device is not None
    assert device.manufacturer == "Kohler Mira"
    assert device.serial_number == SERIAL
    assert device.model_id == "0051"
    assert (dr.CONNECTION_BLUETOOTH, VALVE_ADDRESS) in device.connections
    registry = er.async_get(hass)
    unique_ids = {
        entry.unique_id
        for entry in er.async_entries_for_config_entry(registry, setup_entry.entry_id)
    }
    assert unique_ids == {
        f"{SERIAL}_{key}"
        for key in (
            "outlet_1",
            "outlet_2",
            "target_temperature",
            "target_flow",
            "stop",
            "preset_1",
            "preset_2",
            "running",
            "fault",
            "water_temperature",
            "flow",
            "last_run_duration",
            "last_run_volume",
            "last_run_temperature",
            "error_code",
            "water_total",
            "outlet_1_max_temperature",
            "outlet_1_max_duration",
            "outlet_2_max_temperature",
            "outlet_2_max_duration",
        )
    }
    assert hass.states.get("switch.bathroom_hand_shower").state == "off"
    assert hass.states.get("switch.bathroom_overhead_shower").state == "off"
    assert hass.states.get("button.bathroom_start_default_bathfill") is not None
    assert hass.states.get("sensor.bathroom_water_temperature").state == "25.6"
    assert hass.states.get("sensor.bathroom_error_code") is None  # disabled by default


async def test_setup_retries_when_the_valve_is_out_of_range(
    hass: HomeAssistant, config_entry: MockConfigEntry, valve: FakeValve
) -> None:
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_retries_when_the_valve_does_not_answer(
    hass: HomeAssistant, config_entry: MockConfigEntry, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    valve.silent = True
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload_closes_the_link(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.bathroom_hand_shower"}, blocking=True
    )
    assert valve.is_connected  # held while water runs
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert setup_entry.state is ConfigEntryState.NOT_LOADED
    assert not valve.is_connected
