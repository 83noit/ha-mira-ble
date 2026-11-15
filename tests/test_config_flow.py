"""Config flow: discovery, pairing (success and every failure), reauth and reconfigure."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.mira_ble.const import (
    CONF_OUTLETS,
    CONF_PAIRED_SOURCE,
    CONF_PRESETS,
    CONF_PRODUCT_CODE,
    CONF_SERIAL_NUMBER,
    CONF_VALVE_NAME,
    DOMAIN,
)
from mira_ble import MiraConnectionError, Opcode, PairingRequired
from mira_ble.testing import FakeValve

from .conftest import (
    NAME_WITH_LABEL,
    NAME_WITH_SERIAL,
    PROXY_SOURCE,
    SERIAL,
    VALVE_ADDRESS,
    VALVE_NEW_ADDRESS,
    FakeProxyScanner,
    service_info,
)

FLOW = "homeassistant.config_entries.flow"


async def _start_bluetooth_flow(hass: HomeAssistant, **info: Any) -> dict[str, Any]:
    """Use the flow the Bluetooth integration started from the advert, or start one."""
    await hass.async_block_till_done()
    started = [f for f in hass.config_entries.flow.async_progress() if f["handler"] == DOMAIN]
    if started and not info:
        result = await hass.config_entries.flow.async_configure(started[0]["flow_id"])
    else:
        for flow in started:
            hass.config_entries.flow.async_abort(flow["flow_id"])
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=service_info(**info)
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"
    return result


async def _run_pairing(hass: HomeAssistant, flow_id: str) -> dict[str, Any]:
    """Submit the form, let the progress task finish, and continue."""
    result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "pair"
    assert result["progress_action"] == "pair"
    await hass.async_block_till_done()
    return await hass.config_entries.flow.async_configure(flow_id)


async def test_bluetooth_discovery_pairs_and_creates_the_entry(
    hass: HomeAssistant, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    result = await _start_bluetooth_flow(hass)
    assert result["errors"] == {}
    assert "esphome-proxy-aa01" in result["description_placeholders"]["connectable"]

    result = await _run_pairing(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Bathroom"  # the name stored in the valve
    assert result["result"].unique_id == SERIAL
    data = result["data"]
    assert data[CONF_ADDRESS] == VALVE_ADDRESS
    assert data[CONF_SERIAL_NUMBER] == SERIAL
    assert data[CONF_VALVE_NAME] == "Bathroom"
    assert data[CONF_PRODUCT_CODE] == "0051"
    assert data[CONF_PAIRED_SOURCE] == PROXY_SOURCE
    assert [outlet["number"] for outlet in data[CONF_OUTLETS]] == [1, 2]
    assert data[CONF_OUTLETS][0]["kind"] == "hand_shower"
    assert data[CONF_OUTLETS][1]["max_temperature"] == 46.0
    assert data[CONF_PRESETS] == [
        {"slot": 1, "name": "Default"},
        {"slot": 2, "name": "Default Bathfill"},
    ]
    assert not valve.is_connected
    # Pairing must not change anything on the valve.
    assert {frame.opcode for frame in valve.sent}.isdisjoint(
        {Opcode.SET_OUTLETS, Opcode.RUN_PRESET, Opcode.WRITE_PRESET}
    )


async def test_a_valve_advertising_its_room_name_gets_its_serial_as_identity_after_pairing(
    hass: HomeAssistant, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    proxy.inject(name=NAME_WITH_LABEL)
    result = await _start_bluetooth_flow(hass, name=NAME_WITH_LABEL)
    result = await _run_pairing(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == SERIAL


async def test_a_valve_without_a_readable_serial_keeps_its_address_as_identity(
    hass: HomeAssistant, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    valve.reject.update({Opcode.READ_SERIAL_NUMBER, Opcode.READ_NAME, Opcode.READ_VALVE_CONFIG})
    proxy.inject(name=NAME_WITH_LABEL)
    result = await _start_bluetooth_flow(hass, name=NAME_WITH_LABEL)
    result = await _run_pairing(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == VALVE_ADDRESS
    assert result["title"] == "Bathroom"  # from the advertised name


async def test_a_device_that_is_not_a_mira_valve_is_refused(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=service_info(name="Toothbrush", service_uuids=[]),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"


async def test_a_valve_already_set_up_is_refused(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    for name in (NAME_WITH_SERIAL, NAME_WITH_LABEL):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_BLUETOOTH},
            data=service_info(name=name),
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"


async def test_an_advertisement_alone_never_repoints_an_entry(
    hass: HomeAssistant, config_entry: MockConfigEntry, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    """The serial number is broadcast in the name: anyone in range could send it."""
    config_entry.add_to_hass(hass)
    proxy.inject(VALVE_NEW_ADDRESS)
    result = await _start_bluetooth_flow(hass, address=VALVE_NEW_ADDRESS)
    assert "already set up" in result["description_placeholders"]["moved"]
    assert config_entry.data[CONF_ADDRESS] == VALVE_ADDRESS  # untouched so far
    # Pairing reads the serial number over the bonded link; only then does the entry move,
    # taking the new pairing with it.
    valve.source = "00:11:22:33:CC:03"
    result = await _run_pairing(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "moved"
    assert config_entry.data[CONF_ADDRESS] == VALVE_NEW_ADDRESS
    assert config_entry.data[CONF_PAIRED_SOURCE] == "00:11:22:33:CC:03"
    assert config_entry.unique_id == SERIAL


async def test_pairing_recognises_a_set_up_valve_by_its_serial_and_moves_the_entry(
    hass: HomeAssistant, config_entry: MockConfigEntry, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    """The room-name form hides the serial until the valve has been read."""
    config_entry.add_to_hass(hass)
    proxy.inject(VALVE_NEW_ADDRESS, name=NAME_WITH_LABEL)
    result = await _start_bluetooth_flow(hass, address=VALVE_NEW_ADDRESS, name=NAME_WITH_LABEL)
    result = await _run_pairing(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "moved"
    assert config_entry.data[CONF_ADDRESS] == VALVE_NEW_ADDRESS


async def test_user_flow_lists_valves_and_pairs(
    hass: HomeAssistant, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    proxy.inject("AA:BB:CC:DD:99:99", name="Toothbrush")
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: VALVE_ADDRESS}
    )
    assert result["step_id"] == "bluetooth_confirm"
    result = await _run_pairing(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == SERIAL


async def test_user_flow_without_valves(
    hass: HomeAssistant, config_entry: MockConfigEntry, proxy: FakeProxyScanner
) -> None:
    config_entry.add_to_hass(hass)  # the only valve in range is already set up
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_no_connectable_adapter_then_one_appears(
    hass: HomeAssistant, valve: FakeValve
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=service_info()
    )
    assert result["errors"] == {"base": "no_connectable_adapter"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "no_connectable_adapter"}
    with (
        patch(
            "custom_components.mira_ble.config_flow._connectable_scanners",
            return_value=["proxy (-60 dBm)"],
        ),
        patch(
            "custom_components.mira_ble.config_flow.bluetooth.async_ble_device_from_address",
            return_value=service_info().device,
        ),
    ):
        result = await _run_pairing(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PAIRED_SOURCE] == PROXY_SOURCE  # the adapter actually used


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (PairingRequired("Pairing failed due to error: 82", 82), "not_in_pairing_mode"),
        (MiraConnectionError("no connection slot"), "cannot_connect"),
        (TimeoutError(), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_pairing_failures_are_shown_and_a_retry_completes_the_flow(
    hass: HomeAssistant,
    proxy: FakeProxyScanner,
    valve: FakeValve,
    failure: Exception,
    error: str,
) -> None:
    result = await _start_bluetooth_flow(hass)
    valve.connect_error = failure
    result = await _run_pairing(hass, result["flow_id"])
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair_failed"
    assert result["errors"] == {"base": error}
    assert not valve.is_connected
    valve.connect_error = None
    result = await _run_pairing(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == SERIAL


async def test_a_valve_that_connects_but_does_not_answer_is_cannot_connect(
    hass: HomeAssistant, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    result = await _start_bluetooth_flow(hass)
    valve.silent = True
    result = await _run_pairing(hass, result["flow_id"])
    assert result["errors"] == {"base": "cannot_connect"}
    assert "RequestTimeout" in result["description_placeholders"]["reason"]
    valve.silent = False
    result = await _run_pairing(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_the_valve_leaving_range_before_pairing_starts(
    hass: HomeAssistant, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    result = await _start_bluetooth_flow(hass)
    with patch(
        "custom_components.mira_ble.config_flow.bluetooth.async_ble_device_from_address",
        return_value=None,
    ):
        result = await _run_pairing(hass, result["flow_id"])
    assert result["errors"] == {"base": "no_connectable_adapter"}
    result = await _run_pairing(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY


# --- reauth and reconfigure -------------------------------------------------------------------


async def test_reauth_pairs_again_and_keeps_the_entry(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    coordinator = setup_entry.runtime_data
    result = await setup_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {}
    paused: list[bool] = []
    connect = valve.connect

    with (
        patch("custom_components.mira_ble.config_flow.remove_bluez_bond", AsyncMock()) as remove,
        patch.object(
            type(valve),
            "connect",
            lambda _self: (paused.append(coordinator._paused), connect())[1],
        ),
    ):
        result = await _run_pairing(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert paused[0] is True  # polling was held while the flow owned the connection
    remove.assert_not_awaited()  # the existing bond worked, so nothing was removed
    await hass.async_block_till_done()
    assert setup_entry.unique_id == SERIAL
    assert setup_entry.state is config_entries.ConfigEntryState.LOADED


async def test_reconfigure_re_reads_presets(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    del valve.presets[2]
    result = await setup_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    with patch("custom_components.mira_ble.config_flow.remove_bluez_bond", AsyncMock()):
        result = await _run_pairing(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert setup_entry.data[CONF_PRESETS] == [{"slot": 1, "name": "Default"}]
    assert hass.states.get("button.bathroom_start_default_bathfill") is None


async def test_reauth_when_the_valve_is_out_of_range_then_back(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    with patch(
        "custom_components.mira_ble.config_flow.bluetooth.async_last_service_info",
        return_value=None,
    ):
        result = await setup_entry.start_reauth_flow(hass)
        assert result["errors"] == {"base": "no_longer_in_range"}
    with patch("custom_components.mira_ble.config_flow._connectable_scanners", return_value=[]):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["errors"] == {"base": "no_connectable_adapter"}
    with patch("custom_components.mira_ble.config_flow.remove_bluez_bond", AsyncMock()):
        result = await _run_pairing(hass, result["flow_id"])
    assert result["reason"] == "reauth_successful"


async def test_reauth_refuses_a_different_valve(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    valve.serial = b"9999888877776666" + bytes(2)
    result = await setup_entry.start_reauth_flow(hass)
    with patch("custom_components.mira_ble.config_flow.remove_bluez_bond", AsyncMock()):
        result = await _run_pairing(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert setup_entry.data[CONF_SERIAL_NUMBER] == SERIAL
