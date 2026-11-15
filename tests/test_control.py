"""Switches, numbers, buttons and the start action against the simulated valve."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.mira_ble.const import DOMAIN, RUNNING_INTERVAL, STOP_ATTEMPTS
from mira_ble import Opcode, RequestTimeout, protocol
from mira_ble.testing import PRESET_SHOWER, STOP_FRAME, FakeValve

HAND = "switch.bathroom_hand_shower"
OVERHEAD = "switch.bathroom_overhead_shower"
TEMPERATURE = "number.bathroom_target_temperature"
FLOW = "number.bathroom_target_flow"
STOP = "button.bathroom_stop"
RUNNING = "binary_sensor.bathroom_running"


async def _call(
    hass: HomeAssistant, domain: str, service: str, entity_id: str, **data: Any
) -> None:
    await hass.services.async_call(domain, service, {"entity_id": entity_id, **data}, blocking=True)


def _commands(valve: FakeValve) -> list[bytes]:
    return [
        frame.encode()
        for frame in valve.sent
        if frame.opcode in (Opcode.SET_OUTLETS, Opcode.RUN_PRESET)
    ]


# --- starting from rest -----------------------------------------------------------------------


async def test_a_start_sends_exactly_what_the_numbers_show(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """Outlet 1's defaults at first; never the valve's resting values."""
    assert hass.states.get(TEMPERATURE).state == "38.0"
    assert hass.states.get(FLOW).state == "12.0"
    await _call(hass, "switch", "turn_on", HAND)
    assert (valve.outlets, valve.target_tenths, valve.flow_byte) == (1, 380, 48)
    assert hass.states.get(HAND).state == "on"
    assert hass.states.get(OVERHEAD).state == "off"
    assert hass.states.get(RUNNING).state == "on"
    assert valve.is_connected  # the link is held while water runs


async def test_a_start_does_not_inherit_what_the_last_person_used_at_the_wall(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """A Platinum keeps the last target and flow in its idle state."""
    valve.idle_target_tenths, valve.idle_flow_byte = 440, 60
    await setup_entry.runtime_data.async_refresh()
    assert hass.states.get(TEMPERATURE).state == "38.0"
    await _call(hass, "switch", "turn_on", HAND)
    assert (valve.target_tenths, valve.flow_byte) == (380, 48)


async def test_numbers_set_the_next_start_while_idle(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    valve.sent.clear()
    await _call(hass, "number", "set_value", TEMPERATURE, value=40.5)
    await _call(hass, "number", "set_value", FLOW, value=10)
    assert valve.sent == []  # nothing is written while the valve is idle
    assert hass.states.get(TEMPERATURE).state == "40.5"
    await _call(hass, "switch", "turn_on", OVERHEAD)
    assert (valve.target_tenths, valve.flow_byte) == (405, 40)
    assert hass.states.get(TEMPERATURE).state == "40.5"


async def test_each_outlets_own_limits_cap_a_start(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    state = hass.states.get(TEMPERATURE)
    assert (state.attributes["min"], state.attributes["max"]) == (30.0, 46.0)
    await _call(hass, "number", "set_value", TEMPERATURE, value=46)
    await _call(hass, "switch", "turn_on", HAND)  # outlet 1 allows 45
    assert valve.target_tenths == 450
    await _call(hass, "button", "press", STOP)
    await _call(hass, "switch", "turn_on", OVERHEAD)  # outlet 2 allows 46
    assert valve.target_tenths == 460


async def test_the_flow_is_clamped_to_the_started_outlets_range(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    setup_entry.runtime_data.flow_setpoint = 2.0  # below every outlet's minimum of 4
    await _call(hass, "switch", "turn_on", HAND)
    assert valve.flow_byte == 16


# --- changing a running shower ----------------------------------------------------------------


async def test_the_second_outlet_is_added_to_the_first(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    await _call(hass, "switch", "turn_on", HAND)
    await _call(hass, "switch", "turn_on", OVERHEAD)
    assert valve.outlets == 3
    await _call(hass, "switch", "turn_off", HAND)
    assert valve.outlets == 2
    assert hass.states.get(HAND).state == "off"
    assert hass.states.get(OVERHEAD).state == "on"


async def test_closing_one_outlet_keeps_the_temperature_the_valve_is_running_at(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """A stored setpoint nobody sent once heated a wall-started shower from 36 to 45 C."""
    await _call(hass, "number", "set_value", TEMPERATURE, value=45)  # idle: stored, not sent
    valve.outlets, valve.target_tenths, valve.flow_byte = 3, 360, 40  # started at the wall
    await _call(hass, "switch", "turn_off", HAND)
    assert valve.outlets == 2
    assert (valve.target_tenths, valve.flow_byte) == (360, 40)


async def test_adding_an_outlet_keeps_the_running_temperature_and_flow(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    valve.outlets, valve.target_tenths, valve.flow_byte = 2, 400, 40  # started at the wall
    await _call(hass, "switch", "turn_on", HAND)
    assert valve.outlets == 3
    assert (valve.target_tenths, valve.flow_byte) == (400, 40)


async def test_adding_an_outlet_can_lower_the_temperature_to_its_own_maximum_never_raise_it(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    valve.outlets, valve.target_tenths, valve.flow_byte = 2, 460, 40  # outlet 2 at its 46 C
    await _call(hass, "switch", "turn_on", HAND)  # outlet 1 allows 45
    assert (valve.outlets, valve.target_tenths) == (3, 450)


async def test_a_shower_running_cold_is_not_heated_by_an_outlet_change(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """Target zero is the app's "cold"; it was once read as unknown and replaced by 38 C."""
    valve.outlets, valve.target_tenths, valve.flow_byte = 3, 0, 40
    await _call(hass, "switch", "turn_off", HAND)
    assert (valve.outlets, valve.target_tenths) == (2, 0)


async def test_a_switch_builds_on_the_valves_present_state_not_the_last_poll(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    valve.outlets, valve.target_tenths, valve.flow_byte = 2, 400, 40  # since the last poll
    await _call(hass, "switch", "turn_on", HAND)
    assert valve.outlets == 3


async def test_turning_on_an_outlet_that_is_already_on_sends_no_command(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    await _call(hass, "switch", "turn_on", HAND)
    valve.sent.clear()
    await _call(hass, "switch", "turn_on", HAND)
    assert _commands(valve) == []


async def test_turning_off_an_outlet_that_is_not_open_leaves_the_others_alone(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    await _call(hass, "switch", "turn_on", OVERHEAD)
    valve.sent.clear()
    await _call(hass, "switch", "turn_off", HAND)
    assert _commands(valve) == []
    assert valve.outlets == 2


async def test_numbers_apply_at_once_while_running(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    await _call(hass, "switch", "turn_on", HAND)
    await _call(hass, "number", "set_value", TEMPERATURE, value=41)
    assert (valve.target_tenths, valve.outlets) == (410, 1)
    await _call(hass, "number", "set_value", FLOW, value=10)
    assert valve.flow_byte == 40
    assert hass.states.get(FLOW).state == "10.0"
    assert hass.states.get(TEMPERATURE).state == "41.0"


async def test_a_temperature_change_that_failed_is_not_applied_by_a_later_command(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    await _call(hass, "switch", "turn_on", HAND)
    valve.reject.add(Opcode.SET_OUTLETS)
    with pytest.raises(HomeAssistantError):
        await _call(hass, "number", "set_value", TEMPERATURE, value=44)
    valve.reject.clear()
    assert setup_entry.runtime_data.temperature_setpoint == 38.0
    await _call(hass, "switch", "turn_on", OVERHEAD)
    assert valve.target_tenths == 380  # the value the valve is running at, not the failed 44


async def test_a_number_changed_after_the_valve_stopped_at_the_wall_waits_for_the_next_start(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    await _call(hass, "switch", "turn_on", HAND)
    valve.outlets = 0  # stopped at the wall; Home Assistant has not polled yet
    await _call(hass, "number", "set_value", TEMPERATURE, value=42)
    assert valve.outlets == 0
    assert hass.states.get(TEMPERATURE).state == "42.0"
    assert hass.states.get(HAND).state == "off"


# --- the start action -------------------------------------------------------------------------


async def test_the_start_action_runs_an_outlet_at_values_for_this_start_only(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    await _call(hass, DOMAIN, "start", OVERHEAD, temperature=44, flow=9)
    assert (valve.outlets, valve.target_tenths, valve.flow_byte) == (2, 440, 36)
    await _call(hass, "button", "press", STOP)
    assert hass.states.get(TEMPERATURE).state == "38.0"  # the stored values are untouched
    assert hass.states.get(FLOW).state == "12.0"


async def test_the_start_action_defaults_to_the_numbers(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    await _call(hass, DOMAIN, "start", HAND)
    assert (valve.outlets, valve.target_tenths, valve.flow_byte) == (1, 380, 48)


async def test_the_start_action_refuses_values_the_outlet_does_not_allow_before_connecting(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    connects = valve.connects
    with pytest.raises(ServiceValidationError, match="above this outlet's maximum of 45"):
        await _call(hass, DOMAIN, "start", HAND, temperature=46)
    with pytest.raises(ServiceValidationError, match="outside this outlet's range"):
        await _call(hass, DOMAIN, "start", HAND, flow=20)
    assert valve.connects == connects
    await _call(hass, DOMAIN, "start", OVERHEAD, temperature=46)  # outlet 2 allows it
    assert valve.target_tenths == 460


# --- stopping ---------------------------------------------------------------------------------


async def test_closing_the_last_outlet_stops_and_releases_the_link(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    await _call(hass, "switch", "turn_on", HAND)
    await _call(hass, "switch", "turn_off", HAND)
    assert valve.outlets == 0
    assert _commands(valve)[-1] == STOP_FRAME
    assert not valve.is_connected
    assert hass.states.get(RUNNING).state == "off"


async def test_stop_button(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    await _call(hass, "switch", "turn_on", HAND)
    await _call(hass, "button", "press", STOP)
    assert valve.outlets == 0
    assert hass.states.get(HAND).state == "off"


async def test_a_stop_the_valve_ignored_is_an_error_and_running_stays_on(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    await _call(hass, "switch", "turn_on", HAND)
    valve.ignore_commands = True
    with pytest.raises(HomeAssistantError, match="did not follow"):
        await _call(hass, "button", "press", STOP)
    assert hass.states.get(RUNNING).state == "on"
    assert hass.states.get(HAND).state == "on"
    assert _commands(valve).count(STOP_FRAME) == STOP_ATTEMPTS  # tried on fresh attempts


async def test_stop_is_retried_on_a_fresh_connection(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    await _call(hass, "switch", "turn_on", HAND)
    connects = valve.connects
    refusals = iter([True, False])
    original = FakeValve._set_outlets

    def flaky(self: FakeValve, payload: bytes) -> bytes:
        if not any(payload) and next(refusals):
            return b"\x80"
        return original(self, payload)

    with patch.object(FakeValve, "_set_outlets", flaky):
        await _call(hass, "button", "press", STOP)
    assert valve.outlets == 0
    assert hass.states.get(RUNNING).state == "off"
    assert valve.connects == connects  # the link was still up for the retry


async def test_a_platinum_preset_shows_as_running_and_can_be_stopped(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """Measured on a Platinum: a running preset has the held bit and no outlet bit, and
    only "run preset 0" stopped it."""
    valve.platinum = valve.zero_stop_ignored = True
    await _call(hass, "button", "press", "button.bathroom_start_default")
    assert valve.active
    assert hass.states.get(RUNNING).state == "on"
    assert hass.states.get(HAND).state == "off"  # no outlet bit is reported
    assert float(hass.states.get("sensor.bathroom_flow").state) > 0
    assert valve.is_connected  # still being watched closely
    assert setup_entry.runtime_data.update_interval == timedelta(seconds=RUNNING_INTERVAL)
    await _call(hass, "button", "press", STOP)
    assert not valve.active
    assert hass.states.get(RUNNING).state == "off"
    assert _commands(valve)[-2:] == [STOP_FRAME, protocol.preset_stop_request().encode()]


async def test_turning_off_a_switch_during_a_held_run_stops_the_valve(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    valve.platinum = True
    await _call(hass, "button", "press", "button.bathroom_start_default")
    await _call(hass, "switch", "turn_off", HAND)
    assert not valve.active


# --- presets ----------------------------------------------------------------------------------


async def test_preset_button(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    await _call(hass, "button", "press", "button.bathroom_start_default_bathfill")
    assert (valve.outlets, valve.target_tenths) == (1, 420)
    assert hass.states.get(HAND).state == "on"
    assert hass.states.get(TEMPERATURE).state == "42.0"  # the valve's own target while running


async def test_a_preset_that_was_renamed_in_the_app_is_not_run(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """The button says "Start Default"; whatever now sits in the slot is something else."""
    renamed = bytearray(PRESET_SHOWER)
    renamed[1:8] = b"Scalded"
    valve.presets[1] = bytes(renamed)
    with pytest.raises(ServiceValidationError, match="no longer"):
        await _call(hass, "button", "press", "button.bathroom_start_default")
    assert _commands(valve) == []


async def test_a_preset_hotter_than_its_outlet_allows_is_not_run(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    hot = bytearray(PRESET_SHOWER)
    hot[37], hot[38] = 0x09, 0xE0  # outlet 2, 48.0 C; outlet 2 allows 46
    valve.presets[1] = bytes(hot)
    with pytest.raises(ServiceValidationError, match="above its outlet's maximum of 46"):
        await _call(hass, "button", "press", "button.bathroom_start_default")
    assert _commands(valve) == []


async def test_a_preset_that_has_been_deleted_or_cannot_be_read_is_not_run(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    del valve.presets[1]
    with pytest.raises(ServiceValidationError, match="no longer"):
        await _call(hass, "button", "press", "button.bathroom_start_default")
    valve.ignore.add(Opcode.READ_PRESET)
    with pytest.raises(ServiceValidationError, match="could not be read"):
        await _call(hass, "button", "press", "button.bathroom_start_default_bathfill")
    assert _commands(valve) == []


# --- failures are told truthfully -------------------------------------------------------------


async def test_a_start_that_cannot_be_confirmed_is_stopped_and_says_so(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """Water may have started; reporting "failed" and walking away left it running."""
    valve.lag = 1000  # the start never shows in the state within the window
    with pytest.raises(HomeAssistantError, match="stopped as a precaution"):
        await _call(hass, "switch", "turn_on", HAND)
    assert STOP_FRAME in _commands(valve)


async def test_a_start_whose_stop_cannot_be_confirmed_tells_the_user_to_check(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    valve.lag = 1000
    polls = setup_entry.runtime_data.poll_count
    with (
        patch(
            "custom_components.mira_ble.coordinator.MiraValve.stop",
            side_effect=RequestTimeout("Bathroom: no reply"),
        ),
        pytest.raises(HomeAssistantError, match="CHECK THE SHOWER"),
    ):
        await _call(hass, "switch", "turn_on", HAND)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert setup_entry.runtime_data.poll_count > polls  # a fresh reading was asked for at once


async def test_a_start_that_took_effect_late_is_noticed_at_once(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """The start shows only after the confirmation window and the stop cannot be confirmed:
    the next reading, requested immediately, shows the water running."""
    valve.lag = 1000
    with (
        patch(
            "custom_components.mira_ble.coordinator.MiraValve.stop",
            side_effect=RequestTimeout("Bathroom: no reply"),
        ),
        pytest.raises(HomeAssistantError),
    ):
        await _call(hass, "switch", "turn_on", HAND)
    valve.lag, valve._queued, valve.outlets, valve.target_tenths = 0, [], 1, 380
    await hass.async_block_till_done(wait_background_tasks=True)
    await setup_entry.runtime_data.async_refresh()
    assert hass.states.get(HAND).state == "on"
    assert hass.states.get(RUNNING).state == "on"
    assert valve.is_connected


async def test_an_outlet_change_that_fails_shows_what_the_valve_reports_and_does_not_stop_it(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """A shower someone is standing in must not be cut off because a change failed."""
    await _call(hass, "switch", "turn_on", HAND)
    valve.ignore_commands = True
    with pytest.raises(HomeAssistantError, match="did not follow"):
        await _call(hass, "switch", "turn_on", OVERHEAD)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert valve.outlets == 1
    assert STOP_FRAME not in _commands(valve)
    assert hass.states.get(HAND).state == "on"
    assert hass.states.get(OVERHEAD).state == "off"
    assert valve.is_connected  # water is running: the link is kept


async def test_a_rejected_command_is_an_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    valve.reject.add(Opcode.SET_OUTLETS)
    with pytest.raises(HomeAssistantError, match="CommandRejected"):
        await _call(hass, "switch", "turn_on", HAND)


async def test_a_silent_valve_is_an_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    valve.silent = True
    with pytest.raises(HomeAssistantError, match="RequestTimeout"):
        await _call(hass, "button", "press", STOP)


async def test_a_command_with_no_path_to_the_valve_is_an_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    with (
        patch(
            "custom_components.mira_ble.coordinator.bluetooth.async_ble_device_from_address",
            return_value=None,
        ),
        pytest.raises(HomeAssistantError, match="can currently connect"),
    ):
        await _call(hass, "switch", "turn_on", HAND)


async def test_commands_are_refused_while_a_pairing_flow_owns_the_valve(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    setup_entry.runtime_data.pause_polling()
    connects = valve.connects
    with pytest.raises(HomeAssistantError, match="being paired"):
        await _call(hass, "switch", "turn_on", HAND)
    assert valve.connects == connects
