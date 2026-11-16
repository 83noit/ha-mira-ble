"""Presets on a valve that warms up and holds first, as a Mira Platinum was measured to."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed

from custom_components.mira_ble.const import DOMAIN, RUNNING_INTERVAL, SERVICE_RUN_PRESET
from mira_ble import Opcode, protocol
from mira_ble.testing import FakeValve

PRESET = "button.bathroom_start_default"
STOP = "button.bathroom_stop"
HAND = "switch.bathroom_hand_shower"
PHASE = "sensor.bathroom_phase"
REMAINING = "sensor.bathroom_preset_time_remaining"
RUNNING_PRESET = "sensor.bathroom_running_preset"
RUNNING = "binary_sensor.bathroom_running"
B1 = protocol.run_preset_request(1).encode()


async def _call(
    hass: HomeAssistant, domain: str, service: str, entity_id: str, **data: Any
) -> None:
    await hass.services.async_call(domain, service, {"entity_id": entity_id, **data}, blocking=True)


async def _poll(hass: HomeAssistant, times: int = 1) -> None:
    for _ in range(times):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=RUNNING_INTERVAL + 1))
        await hass.async_block_till_done(wait_background_tasks=True)


def _presets_sent(valve: FakeValve) -> list[bytes]:
    return [frame.encode() for frame in valve.sent if frame.opcode == Opcode.RUN_PRESET]


def _state(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state


async def test_at_rest_the_phase_is_idle_and_nothing_counts_down(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    assert _state(hass, PHASE) == "rest"
    assert _state(hass, REMAINING) == "unknown"
    assert _state(hass, RUNNING_PRESET) == "unknown"
    assert _state(hass, "sensor.bathroom_water_used_this_run") == "0"


async def test_a_press_warms_up_and_a_second_press_runs_the_preset(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """As at the wall controller: start, wait for ready, start again."""
    valve.warm_up = 3
    await _call(hass, "button", "press", PRESET)
    assert _state(hass, PHASE) == "warming_up"
    assert _state(hass, RUNNING) == "on"
    assert _state(hass, REMAINING) == "unknown"
    await _poll(hass, 3)
    assert _state(hass, PHASE) == "paused"
    assert _state(hass, RUNNING) == "off"
    assert _presets_sent(valve) == [B1]  # nothing carried it on
    valve.volume = 2
    await _call(hass, "button", "press", PRESET)
    assert _state(hass, PHASE) == "preset"
    assert _state(hass, RUNNING_PRESET) == "Default"
    assert _state(hass, REMAINING) == "30.0"  # minutes
    assert _state(hass, "sensor.bathroom_water_used_this_run") == "2"
    assert _presets_sent(valve) == [B1, B1]


async def test_run_preset_carries_on_after_the_warm_up(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    valve.warm_up = 3
    await _call(hass, DOMAIN, SERVICE_RUN_PRESET, PRESET)
    assert _state(hass, PHASE) == "warming_up"
    assert _presets_sent(valve) == [B1]
    await _poll(hass, 4)
    assert _state(hass, PHASE) == "preset"
    assert _state(hass, RUNNING_PRESET) == "Default"
    assert _presets_sent(valve) == [B1, B1]
    await _poll(hass, 2)
    assert _presets_sent(valve) == [B1, B1]  # once only


async def test_run_preset_is_not_cancelled_by_the_water_draining_after_the_warm_up(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """Found on a Mira Platinum: held, no outlet bit, flow still read for two seconds."""
    valve.warm_up, valve.drain = 3, 3
    await _call(hass, DOMAIN, SERVICE_RUN_PRESET, PRESET)
    seen = []
    for _ in range(10):
        await _poll(hass)
        seen.append((_state(hass, PHASE), valve.held and valve.flowing, len(_presets_sent(valve))))
    # Polled while the last water drained: shown as paused, and nothing was sent then.
    assert ("paused", True, 1) in seen
    assert _state(hass, PHASE) == "preset"
    assert _presets_sent(valve) == [B1, B1]


async def test_run_preset_on_a_valve_with_no_warm_up_sends_one_command(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    await _call(hass, DOMAIN, SERVICE_RUN_PRESET, PRESET)
    assert _state(hass, PHASE) == "running"
    await _poll(hass, 3)
    assert _presets_sent(valve) == [B1]
    assert setup_entry.runtime_data._through is None


async def test_a_stop_during_the_warm_up_cancels_the_rest_of_the_preset(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """The stop leaves the valve held, which must not be taken for the end of the warm-up."""
    valve.warm_up = 50
    await _call(hass, DOMAIN, SERVICE_RUN_PRESET, PRESET)
    await _call(hass, "button", "press", STOP)
    assert valve.held
    assert not valve.running
    await _poll(hass, 3)
    assert _presets_sent(valve) == [B1]
    assert _state(hass, PHASE) == "paused"


async def test_an_outlet_switched_on_during_the_warm_up_cancels_the_rest_of_the_preset(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    valve.warm_up = 50
    await _call(hass, DOMAIN, SERVICE_RUN_PRESET, PRESET)
    await _call(hass, "switch", "turn_on", HAND)
    await _poll(hass, 2)
    assert _presets_sent(valve) == [B1]
    assert _state(hass, PHASE) == "running"


async def test_a_warm_up_that_ends_at_rest_cancels_the_rest_of_the_preset(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """Stopped at the wall and timed out between two polls."""
    valve.warm_up = 50
    await _call(hass, DOMAIN, SERVICE_RUN_PRESET, PRESET)
    valve.rest()
    await _poll(hass)
    assert setup_entry.runtime_data._through is None
    assert _presets_sent(valve) == [B1]


async def test_a_warm_up_that_never_ends_is_not_waited_on_for_ever(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    valve.warm_up = 10**6
    with patch("custom_components.mira_ble.coordinator.THROUGH_TIMEOUT", -1.0):
        await _call(hass, DOMAIN, SERVICE_RUN_PRESET, PRESET)
    await _poll(hass)
    assert setup_entry.runtime_data._through is None
    assert _state(hass, PHASE) == "warming_up"  # still shown truthfully; Stop is there


async def test_a_preset_the_valve_will_not_start_after_its_warm_up_is_logged_not_raised(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    valve: FakeValve,
    caplog: pytest.LogCaptureFixture,
) -> None:
    valve.warm_up = 3
    await _call(hass, DOMAIN, SERVICE_RUN_PRESET, PRESET)
    valve.ignore_commands = True
    await _poll(hass, 4)
    assert "did not start after its warm-up" in caplog.text
    assert setup_entry.runtime_data._through is None
    assert _state(hass, PHASE) == "paused"
    assert _state(hass, RUNNING) == "off"


async def test_run_preset_refuses_the_stop_button(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    with pytest.raises(ServiceValidationError, match="not a preset"):
        await _call(hass, DOMAIN, SERVICE_RUN_PRESET, STOP)
    assert not valve.sent or all(f.opcode != Opcode.RUN_PRESET for f in valve.sent)
