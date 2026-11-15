"""Pairing-flow rules that came out of review: nothing is destroyed on suspicion."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.mira_ble.const import (
    CONF_OUTLETS,
    CONF_PRESETS,
    CONF_PRESETS_UNREADABLE,
    CONF_READ_ERRORS,
    CONF_SERIAL_NUMBER,
    DOMAIN,
)
from mira_ble import LEGACY_SERVICE_UUID, SERVICE_UUID, Opcode, PairingRequired
from mira_ble.testing import SERIAL_BLANK, STATE_RUNNING, FakeValve

from .conftest import (
    NAME_WITH_LABEL,
    SERIAL,
    VALVE_ADDRESS,
    FakeProxyScanner,
    service_info,
)
from .test_config_flow import _run_pairing, _start_bluetooth_flow

REMOVE = "custom_components.mira_ble.config_flow.remove_bluez_bond"

# --- what counts as a Mira valve --------------------------------------------------------------


async def test_another_device_sharing_the_service_uuid_is_not_offered(hass: HomeAssistant) -> None:
    """The UUID is not unique to these valves; a real user saw strangers discovered."""
    for name in ("Kohler Remote", None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_BLUETOOTH},
            data=service_info(name=name, service_uuids=[SERVICE_UUID]),
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "not_supported"


async def test_a_valve_of_the_older_generation_is_refused_whatever_its_name(
    hass: HomeAssistant,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=service_info(name="Mira 0001 Bathroom", service_uuids=[LEGACY_SERVICE_UUID]),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"


async def test_a_valve_that_does_not_advertise_the_service_is_still_accepted_by_name(
    hass: HomeAssistant, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    result = await _start_bluetooth_flow(hass, name=NAME_WITH_LABEL, service_uuids=[])
    assert result["step_id"] == "bluetooth_confirm"


async def test_a_blank_serial_is_not_an_identity(
    hass: HomeAssistant, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    """Two valves never given a serial both report zeroes; they must not be merged."""
    valve.serial = SERIAL_BLANK
    proxy.inject(name=NAME_WITH_LABEL)
    result = await _start_bluetooth_flow(hass, name=NAME_WITH_LABEL)
    result = await _run_pairing(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == VALVE_ADDRESS
    assert result["data"][CONF_SERIAL_NUMBER] is None


# --- pairing reads ----------------------------------------------------------------------------


async def test_pairing_survives_a_valve_silent_on_optional_reads(
    hass: HomeAssistant, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    """A Platinum at rest ignores preset reads; pairing must still finish."""
    valve.ignore.update({Opcode.READ_PRESET, Opcode.READ_MANUFACTURED})
    result = await _start_bluetooth_flow(hass)
    result = await _run_pairing(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert [outlet["number"] for outlet in data[CONF_OUTLETS]] == [1, 2]
    assert data[CONF_PRESETS] == []
    assert data[CONF_PRESETS_UNREADABLE] is True
    assert data[CONF_READ_ERRORS] == {"preset_1": "no answer", "manufactured": "no answer"}
    assert result["result"].unique_id == SERIAL


async def test_pairing_fails_when_the_valve_does_not_answer_the_state_read(
    hass: HomeAssistant, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    """The one read every valve must answer: it proves the link and the protocol."""
    valve.ignore.add(Opcode.READ_STATE)
    result = await _start_bluetooth_flow(hass)
    result = await _run_pairing(hass, result["flow_id"])
    assert result["errors"] == {"base": "cannot_connect"}
    valve.ignore.clear()
    result = await _run_pairing(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_a_stray_frame_during_pairing_does_not_corrupt_the_identity(
    hass: HomeAssistant, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    """One unsolicited frame once stored the room name as the serial number."""
    valve.unsolicited.append(STATE_RUNNING)
    result = await _start_bluetooth_flow(hass)
    result = await _run_pairing(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == SERIAL
    assert result["title"] == "Bathroom"
    assert len(result["data"][CONF_OUTLETS]) == 2


async def test_a_link_that_cannot_be_encrypted_is_reported_as_such(
    hass: HomeAssistant, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    result = await _start_bluetooth_flow(hass)
    valve.connect_error = PairingRequired("Insufficient authentication", 5, refused=False)
    with patch(REMOVE, AsyncMock(return_value=True)) as remove:
        result = await _run_pairing(hass, result["flow_id"])
    assert result["errors"] == {"base": "not_encrypted"}
    remove.assert_not_awaited()  # weak evidence removes nothing
    valve.connect_error = None
    result = await _run_pairing(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY


# --- bonds ------------------------------------------------------------------------------------


async def test_reconfigure_with_a_working_bond_removes_nothing(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """Submitting out of curiosity, valve not in pairing mode, must not strand it."""
    result = await setup_entry.start_reconfigure_flow(hass)
    with patch(REMOVE, AsyncMock(return_value=True)) as remove:
        result = await _run_pairing(hass, result["flow_id"])
    assert result["reason"] == "reconfigure_successful"
    remove.assert_not_awaited()


async def test_a_refused_bond_is_removed_from_local_adapters_and_pairing_retried_once(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    result = await setup_entry.start_reauth_flow(hass)
    attempts: list[int] = []
    connect = FakeValve.connect

    async def refuse_first(self: FakeValve) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise PairingRequired("Pairing failed due to error: 82", 82)
        await connect(self)

    with (
        patch.object(FakeValve, "connect", refuse_first),
        patch(REMOVE, AsyncMock(return_value=True)) as remove,
    ):
        result = await _run_pairing(hass, result["flow_id"])
    assert result["reason"] == "reauth_successful"
    assert len(attempts) >= 2  # refused, retried; the reload after success connects again
    assert remove.await_args_list[0].args[0].address == VALVE_ADDRESS


async def test_a_refusal_with_no_local_bond_to_remove_is_reported_without_a_retry(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """Through a proxy there is nothing to forget; the user must use pairing mode."""
    result = await setup_entry.start_reauth_flow(hass)
    valve.connect_error = PairingRequired("Pairing failed due to error: 82", 82)
    connects = valve.connects
    with patch(REMOVE, AsyncMock(return_value=False)):
        result = await _run_pairing(hass, result["flow_id"])
    assert result["errors"] == {"base": "not_in_pairing_mode"}
    assert valve.connects == connects + 1


# --- never shrinking on a failed re-read ------------------------------------------------------


async def test_a_reconfigure_whose_reads_failed_keeps_the_outlets_presets_and_entities(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """A read that failed is not evidence that an outlet is gone."""
    valve.reject.update({Opcode.READ_OUTLET_CONFIG, Opcode.READ_PRESET})
    result = await setup_entry.start_reconfigure_flow(hass)
    result = await _run_pairing(hass, result["flow_id"])
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert len(setup_entry.data[CONF_OUTLETS]) == 2
    assert len(setup_entry.data[CONF_PRESETS]) == 2
    assert set(setup_entry.data[CONF_READ_ERRORS]) == {"outlet_1", "outlet_2", "preset_1"}
    registry = er.async_get(hass)
    for entity_id in (
        "switch.bathroom_hand_shower",
        "switch.bathroom_overhead_shower",
        "button.bathroom_start_default",
        "button.bathroom_start_default_bathfill",
    ):
        assert registry.async_get(entity_id) is not None, entity_id


async def test_a_preset_deleted_in_the_app_is_removed_on_a_clean_re_read(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    del valve.presets[2]
    result = await setup_entry.start_reconfigure_flow(hass)
    result = await _run_pairing(hass, result["flow_id"])
    await hass.async_block_till_done()
    assert setup_entry.data[CONF_PRESETS] == [{"slot": 1, "name": "Default"}]
    assert er.async_get(hass).async_get("button.bathroom_start_default_bathfill") is None
    assert er.async_get(hass).async_get("switch.bathroom_hand_shower") is not None
