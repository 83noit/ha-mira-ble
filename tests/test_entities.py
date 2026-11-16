"""Entities beyond the basics: restored numbers, the water total, limits, unchecked presets."""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry, mock_restore_cache_with_extra_data

from custom_components.mira_ble.const import CONF_PRESETS, CONF_PRESETS_UNREADABLE, DOMAIN
from custom_components.mira_ble.diagnostics import async_get_config_entry_diagnostics
from mira_ble import Opcode
from mira_ble.testing import FakeValve

from .conftest import PROXY_SOURCE, FakeProxyScanner


async def test_the_fault_sensor_and_the_outlet_limits_are_named(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    assert hass.states.get("binary_sensor.bathroom_fault").state == "off"
    assert hass.states.get("sensor.bathroom_outlet_1_maximum_run_time").state == "30.0"
    assert hass.states.get("sensor.bathroom_outlet_2_maximum_temperature").state == "46.0"


async def test_a_valve_that_does_not_answer_preset_reads_gets_unchecked_slot_buttons_disabled(
    hass: HomeAssistant, config_entry: MockConfigEntry, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    """A Platinum at rest: the presets cannot be listed, so the slots are offered, off by
    default, and run unchecked once the user enables one."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        data={**config_entry.data, CONF_PRESETS: [], CONF_PRESETS_UNREADABLE: True},
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    for slot in (1, 2, 3):
        entry = registry.async_get(f"button.bathroom_start_preset_{slot}")
        assert entry is not None
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    valve.ignore.add(Opcode.READ_PRESET)
    await config_entry.runtime_data.async_run_preset(2)  # what the enabled button would do
    assert valve.outlets == 1


async def test_setpoints_survive_a_restart(
    hass: HomeAssistant, config_entry: MockConfigEntry, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    """A lower temperature chosen for a child must not silently go back to the default."""
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State("number.bathroom_target_temperature", "31.0"),
                {
                    "native_max_value": 46.0,
                    "native_min_value": 30.0,
                    "native_step": 0.5,
                    "native_unit_of_measurement": "°C",
                    "native_value": 31.0,
                },
            ),
            (
                State("number.bathroom_target_flow", "99.0"),  # out of range: ignored
                {
                    "native_max_value": 16.0,
                    "native_min_value": 4.0,
                    "native_step": 0.5,
                    "native_unit_of_measurement": "L/min",
                    "native_value": 99.0,
                },
            ),
        ],
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("number.bathroom_target_temperature").state == "31.0"
    assert hass.states.get("number.bathroom_target_flow").state == "12.0"
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.bathroom_hand_shower"}, blocking=True
    )
    assert valve.target_tenths == 310


async def test_water_used_counts_each_new_run_once(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    total = "sensor.bathroom_water_used"
    state = hass.states.get(total)
    assert state.state == "0.0"  # the record seen at startup is a baseline, not counted
    assert state.attributes["state_class"] == "total_increasing"
    assert state.attributes["device_class"] == "water"
    coordinator = setup_entry.runtime_data
    valve.last_usage = bytes.fromhex("02012c002350")  # 300 s, 35 litres
    await coordinator.async_refresh()
    assert hass.states.get(total).state == "35.0"
    await coordinator.async_refresh()  # the same record again
    assert hass.states.get(total).state == "35.0"
    valve.last_usage = bytes.fromhex("02007800104c")  # 120 s, 16 litres
    await coordinator.async_refresh()
    assert hass.states.get(total).state == "51.0"


async def test_water_used_carries_on_after_a_restart(
    hass: HomeAssistant, config_entry: MockConfigEntry, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State("sensor.bathroom_water_used", "120.0"),
                {"native_value": 120.0, "native_unit_of_measurement": "L"},
            )
        ],
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.bathroom_water_used").state == "120.0"
    valve.last_usage = bytes.fromhex("02007800104c")
    await config_entry.runtime_data.async_refresh()
    assert hass.states.get("sensor.bathroom_water_used").state == "136.0"


async def test_the_start_action_is_registered_with_the_integration_not_a_valve(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    assert hass.services.has_service(DOMAIN, "start")
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert hass.services.has_service(DOMAIN, "start")


async def test_diagnostics_hide_names_people_chose_and_show_what_debugging_needs(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    other = MockConfigEntry(domain="esphome")
    other.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, PROXY_SOURCE)},
        sw_version="2026.10.2",
    )
    diagnostics = await async_get_config_entry_diagnostics(hass, setup_entry)
    text = json.dumps(diagnostics)
    assert "Default" not in text  # preset names are chosen by people
    assert diagnostics["entry"]["data"]["presets"][1] == {"slot": 2, "name": "**REDACTED**"}
    # The ESPHome version is what identifies a proxy that forgets bonds.
    assert diagnostics["bluetooth"]["scanners_hearing_the_valve"][0]["firmware"] == "2026.10.2"
    assert diagnostics["bluetooth"]["last_connection_via"] == "**:**:**:**:AA:01"
    assert diagnostics["state"]["active"] is False
    assert diagnostics["setpoints"] == {"temperature": 38.0, "flow": 12.0}
    assert diagnostics["outlets"][1]["max_duration"] == 1800
    assert diagnostics["polling"]["optional_reads_backing_off"] == {}


async def test_target_flow_moves_in_whole_litres(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Half-litre steps made the slider show "12.0 L/min", too wide for its value box."""
    state = hass.states.get("number.bathroom_target_flow")
    assert state is not None
    assert state.attributes["step"] == 1
    temperature = hass.states.get("number.bathroom_target_temperature")
    assert temperature is not None
    assert temperature.attributes["step"] == 0.5
