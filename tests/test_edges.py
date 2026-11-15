"""Less common paths: timeouts, refusals before sending, unnamed outlets, path guessing."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.mira_ble.config_flow import _likely_path
from custom_components.mira_ble.const import CONF_OUTLETS, CONF_PAIRED_SOURCE
from mira_ble import Frame, NotConfirmed, Opcode
from mira_ble.testing import STOP_FRAME, FakeValve

from .conftest import PROXY_SOURCE, FakeProxyScanner, outlet
from .test_config_flow import _run_pairing, _start_bluetooth_flow

HAND = "switch.bathroom_hand_shower"


async def _turn(hass: HomeAssistant, service: str, entity_id: str = HAND) -> None:
    await hass.services.async_call("switch", service, {"entity_id": entity_id}, blocking=True)


async def test_a_command_that_runs_out_of_time_is_an_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    original = FakeValve.request

    async def slow(self: FakeValve, frame: Frame, **kwargs: Any) -> Frame:
        await asyncio.sleep(0.2)
        return await original(self, frame, **kwargs)

    with (
        patch.object(FakeValve, "request", slow),
        patch("custom_components.mira_ble.coordinator.COMMAND_TIMEOUT", 0.05),
        pytest.raises(HomeAssistantError, match="TimeoutError"),
    ):
        await _turn(hass, "turn_on")


async def test_a_value_the_library_refuses_is_a_validation_error_and_nothing_is_sent(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    setup_entry.runtime_data.temperature_setpoint = 10.0  # the number entity cannot set this
    valve.sent.clear()
    with pytest.raises(ServiceValidationError, match="Refused before sending"):
        await _turn(hass, "turn_on")
    assert Opcode.SET_OUTLETS not in [frame.opcode for frame in valve.sent]


async def test_a_precautionary_stop_the_valve_does_not_follow_tells_the_user_to_check(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    valve.lag = 1000
    with (
        patch(
            "custom_components.mira_ble.coordinator.MiraValve.stop",
            side_effect=NotConfirmed("stop was acknowledged but the valve still runs"),
        ),
        pytest.raises(HomeAssistantError, match="CHECK THE SHOWER"),
    ):
        await _turn(hass, "turn_on")


async def test_turning_off_an_outlet_of_an_idle_valve_sends_nothing(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    valve.sent.clear()
    await _turn(hass, "turn_off")
    assert STOP_FRAME not in [frame.encode() for frame in valve.sent]


async def test_an_outlet_added_to_a_run_reporting_no_flow_uses_the_stored_flow(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """A command with zero flow would be refused; the valve's own value is unusable here."""
    valve.outlets, valve.target_tenths, valve.flow_byte = 2, 380, 0
    await _turn(hass, "turn_on")
    assert (valve.outlets, valve.flow_byte) == (3, 48)


async def test_two_outlets_of_the_same_kind_are_numbered(
    hass: HomeAssistant, config_entry: MockConfigEntry, proxy: FakeProxyScanner
) -> None:
    """Both outlets of a recorded dual valve report "hand shower"."""
    valve = FakeValve(source=PROXY_SOURCE)
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            CONF_OUTLETS: [outlet(1, "hand_shower"), outlet(2, "hand_shower")],
        },
    )
    with patch("custom_components.mira_ble.coordinator.BleTransport", return_value=valve):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert hass.states.get("switch.bathroom_outlet_1") is not None
    assert hass.states.get("switch.bathroom_outlet_2") is not None


async def test_the_pairing_path_is_guessed_when_the_transport_cannot_tell(
    hass: HomeAssistant, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    valve.source = None
    result = await _start_bluetooth_flow(hass)
    result = await _run_pairing(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PAIRED_SOURCE] == PROXY_SOURCE  # the strongest connectable one


async def test_no_connectable_adapter_means_an_unknown_path(hass: HomeAssistant) -> None:
    assert _likely_path(hass, "AA:BB:CC:DD:00:00") == "unknown"


async def test_an_unknowable_pairing_path_is_recorded_as_unknown(
    hass: HomeAssistant, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    valve.source = None
    result = await _start_bluetooth_flow(hass)
    with patch("custom_components.mira_ble.config_flow._likely_path", return_value="unknown"):
        result = await _run_pairing(hass, result["flow_id"])
    assert result["data"][CONF_PAIRED_SOURCE] == "unknown"
