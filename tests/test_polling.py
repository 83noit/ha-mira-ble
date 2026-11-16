"""Polling cadence, availability, optional reads, and what a failing bond leads to."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
import contextlib
from datetime import timedelta
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed

from custom_components.mira_ble.const import (
    CONF_PAIRED_AT,
    CONF_PAIRED_SOURCE,
    DOMAIN,
    FAILURES_BEFORE_UNAVAILABLE,
    IDLE_INTERVAL,
    LINK_BUDGET,
    MAX_PAIRING_REFUSALS,
    OUTAGE_ISSUE_AFTER,
    OUTLET_REFRESH,
    REAUTH_INTERVAL,
    RUNNING_INTERVAL,
    RUNNING_RETRIES,
)
from mira_ble import Opcode, PairingRequired
from mira_ble.testing import OUTLET_1_CONFIG, FakeValve

from .conftest import PROXY_SOURCE, FakeProxyScanner

HAND = "switch.bathroom_hand_shower"
WATER = "sensor.bathroom_water_temperature"
STOP = "button.bathroom_stop"
RUNNING = "binary_sensor.bathroom_running"
OLD = "2026-01-01T00:00:00+00:00"


async def _tick(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done(wait_background_tasks=True)


def _interval(entry: MockConfigEntry) -> float:
    return entry.runtime_data.update_interval.total_seconds()


# --- cadence ----------------------------------------------------------------------------------


async def test_an_idle_valve_is_polled_slowly_over_short_connections(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    assert _interval(setup_entry) == IDLE_INTERVAL
    assert valve.connects == 1
    valve.water_temperature = 21.5
    await _tick(hass, IDLE_INTERVAL + 1)
    assert valve.connects == 2
    assert not valve.is_connected
    assert hass.states.get(WATER).state == "21.5"
    assert hass.states.get("sensor.bathroom_last_run_volume").state == "1"
    assert hass.states.get("sensor.bathroom_last_run_temperature").state == "44.0"


async def test_a_running_valve_is_polled_fast_on_a_new_link_each_time(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    await hass.services.async_call("switch", "turn_on", {"entity_id": HAND}, blocking=True)
    assert _interval(setup_entry) == RUNNING_INTERVAL
    connects = valve.connects
    valve.water_temperature = 37.9
    await _tick(hass, RUNNING_INTERVAL + 1)
    assert valve.connects == connects + 1  # a link of its own: a held one goes slow
    assert not valve.is_connected
    assert hass.states.get(WATER).state == "37.9"
    valve.outlets = 0  # stopped at the wall
    await _tick(hass, RUNNING_INTERVAL + 1)
    assert hass.states.get(HAND).state == "off"
    assert not valve.is_connected
    assert _interval(setup_entry) == IDLE_INTERVAL


async def test_a_run_started_at_the_wall_is_followed_without_changing_the_stored_values(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    valve.outlets, valve.target_tenths, valve.flow_byte = 2, 400, 40
    await _tick(hass, IDLE_INTERVAL + 1)
    assert hass.states.get("switch.bathroom_overhead_shower").state == "on"
    assert hass.states.get("number.bathroom_target_temperature").state == "40.0"
    assert hass.states.get("number.bathroom_target_flow").state == "10.0"
    assert _interval(setup_entry) == RUNNING_INTERVAL
    valve.outlets = 0
    await _tick(hass, RUNNING_INTERVAL + 1)
    # At rest the numbers are Home Assistant's own again, not the wall user's.
    assert hass.states.get("number.bathroom_target_temperature").state == "38.0"
    assert hass.states.get("number.bathroom_target_flow").state == "12.0"


async def test_a_held_valve_with_the_water_off_is_watched_but_not_running(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """Measured on a Platinum: warm-up done or just stopped, held for two minutes."""
    valve.held = True
    valve.volume = 4
    await _tick(hass, IDLE_INTERVAL + 1)
    assert hass.states.get(RUNNING).state == "off"
    assert hass.states.get("sensor.bathroom_flow").state == "0.0"
    assert _interval(setup_entry) == RUNNING_INTERVAL
    valve.rest()
    await _tick(hass, RUNNING_INTERVAL + 1)
    assert _interval(setup_entry) == IDLE_INTERVAL


async def test_water_flowing_with_no_outlet_bit_is_followed_as_running(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """Started at the wall: held bit and measured flow, no outlet bit."""
    valve.held = valve.flowing = True
    valve.target_tenths, valve.flow_byte = 380, 48
    await _tick(hass, IDLE_INTERVAL + 1)
    assert hass.states.get(RUNNING).state == "on"
    assert hass.states.get("sensor.bathroom_flow").state == "12.0"
    assert _interval(setup_entry) == RUNNING_INTERVAL


async def test_a_link_that_has_been_up_too_long_is_not_used_again(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """Measured through a proxy: a held link answers in 7 to 10 s, a new one in 0.3 s."""
    coordinator = setup_entry.runtime_data
    old = await coordinator._async_connected_valve()  # as if something had left a link up
    assert valve.is_connected
    connects = valve.connects
    valve.link_age = LINK_BUDGET + 1
    await coordinator.async_refresh()
    assert valve.connects == connects + 1
    assert old.transport is valve
    assert not valve.is_connected


async def test_a_command_waiting_on_a_poll_takes_over_its_young_link(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """A Stop pressed during a poll must not pay for a second connection."""
    coordinator = setup_entry.runtime_data
    valve.outlets, valve.target_tenths, valve.flow_byte = 1, 380, 32
    original = FakeValve.state_payload
    stops: list[asyncio.Task[None]] = []

    def read_and_press_stop(self: FakeValve) -> bytes:
        if not stops:  # during the poll's read, someone presses Stop
            stops.append(hass.async_create_task(coordinator.async_stop()))
        return original(self)

    connects = valve.connects
    with patch.object(FakeValve, "state_payload", read_and_press_stop):
        await coordinator.async_refresh()
        await asyncio.gather(*stops)
    assert valve.connects == connects + 1  # one link served the poll and the stop
    assert not valve.running
    assert not valve.is_connected


# --- failures ---------------------------------------------------------------------------------


async def test_one_missed_poll_keeps_the_last_reading_and_the_controls(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """One or two missed polls are noise; blanking everything reset automation timers."""
    valve.silent = True
    for _ in range(FAILURES_BEFORE_UNAVAILABLE - 1):
        await _tick(hass, IDLE_INTERVAL + 1)
        assert hass.states.get(WATER).state == "25.6"
        assert hass.states.get(HAND).state == "off"
    await _tick(hass, IDLE_INTERVAL + 1)
    assert hass.states.get(WATER).state == STATE_UNAVAILABLE
    assert hass.states.get(HAND).state == STATE_UNAVAILABLE
    assert hass.states.get(RUNNING).state == STATE_UNAVAILABLE


async def test_stop_and_the_numbers_stay_available_through_an_outage(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """Home Assistant silently skips an action aimed at an unavailable entity."""
    valve.silent = True
    for _ in range(FAILURES_BEFORE_UNAVAILABLE + 1):
        await _tick(hass, IDLE_INTERVAL + 1)
    assert hass.states.get(WATER).state == STATE_UNAVAILABLE
    for control in (
        STOP,
        "number.bathroom_target_temperature",
        "number.bathroom_target_flow",
        "button.bathroom_start_default",
    ):
        assert hass.states.get(control).state != STATE_UNAVAILABLE, control
    valve.silent = False
    valve.outlets, valve.target_tenths, valve.flow_byte = 1, 380, 40  # water is in fact running
    await hass.services.async_call("button", "press", {"entity_id": STOP}, blocking=True)
    assert valve.outlets == 0


async def test_a_failed_poll_during_a_run_is_retried_at_the_running_rate(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """Backing off to a minute while water runs put Stop out of reach."""
    await hass.services.async_call("switch", "turn_on", {"entity_id": HAND}, blocking=True)
    valve.silent = True
    for _ in range(RUNNING_RETRIES):
        await _tick(hass, RUNNING_INTERVAL + 1)
        assert _interval(setup_entry) == RUNNING_INTERVAL
    await _tick(hass, RUNNING_INTERVAL + 1)
    assert _interval(setup_entry) == IDLE_INTERVAL  # then it backs off
    valve.silent = False
    await _tick(hass, IDLE_INTERVAL + 1)
    assert hass.states.get(HAND).state == "on"
    assert _interval(setup_entry) == RUNNING_INTERVAL


async def test_an_outage_is_logged_once_and_recovery_once(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    valve: FakeValve,
    caplog: pytest.LogCaptureFixture,
) -> None:
    valve.silent = True
    await _tick(hass, IDLE_INTERVAL + 1)
    assert "RequestTimeout" in (setup_entry.runtime_data.last_error or "")
    await _tick(hass, IDLE_INTERVAL + 1)
    assert caplog.text.count("Error fetching Bathroom data") == 1
    valve.silent = False
    await _tick(hass, IDLE_INTERVAL + 1)
    assert hass.states.get(WATER).state == "25.6"
    assert caplog.text.count("Fetching Bathroom data recovered") == 1


async def test_no_path_to_the_valve_fails_the_poll(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    with patch(
        "custom_components.mira_ble.coordinator.bluetooth.async_ble_device_from_address",
        return_value=None,
    ):
        await _tick(hass, IDLE_INTERVAL + 1)
    assert "UpdateFailed" in (setup_entry.runtime_data.last_error or "")
    assert setup_entry.runtime_data.consecutive_failures == 1


async def test_polling_fails_honestly_while_a_pairing_flow_owns_the_valve(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    coordinator = setup_entry.runtime_data
    coordinator.pause_polling()
    connects = valve.connects
    await _tick(hass, IDLE_INTERVAL + 1)
    assert valve.connects == connects
    assert not coordinator.last_update_success  # not reported as a fresh reading
    assert hass.states.get(WATER).state == "25.6"  # the grace period covers a short pairing
    coordinator.resume_polling()
    await _tick(hass, IDLE_INTERVAL + 1)
    assert valve.connects == connects + 1
    assert coordinator.last_update_success


# --- optional reads ---------------------------------------------------------------------------


async def test_a_valve_silent_on_the_last_run_read_still_polls_and_backs_off(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """Some valves ignore requests they do not support; one must not fail every poll."""
    coordinator = setup_entry.runtime_data
    valve.ignore.add(Opcode.READ_USAGE)
    asked: list[int] = []
    for _ in range(8):
        valve.sent.clear()
        valve.water_temperature += 0.1
        await _tick(hass, IDLE_INTERVAL + 1)
        asked.append(sum(frame.opcode == Opcode.READ_USAGE for frame in valve.sent))
        assert coordinator.last_update_success
    assert asked == [1, 0, 1, 0, 0, 1, 0, 0]  # skipped for 1, then 2, then 4 polls
    assert hass.states.get("sensor.bathroom_last_run_volume").state == "1"  # the earlier record
    assert coordinator.optional_skips["last_usage"] > 0
    valve.ignore.clear()
    for _ in range(5):
        await _tick(hass, IDLE_INTERVAL + 1)
    assert "last_usage" not in coordinator.optional_skips  # asked every poll again


async def test_the_backoff_of_an_unanswered_read_is_bounded(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    coordinator = setup_entry.runtime_data
    valve.ignore.add(Opcode.READ_USAGE)
    for _ in range(80):
        await _tick(hass, IDLE_INTERVAL + 1)
    assert coordinator._optional_backoff["last_usage"] == 32


async def test_a_refused_last_run_read_does_not_fail_the_poll(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    valve.reject.add(Opcode.READ_USAGE)
    valve.water_temperature = 22.0
    await _tick(hass, IDLE_INTERVAL + 1)
    assert hass.states.get(WATER).state == "22.0"
    assert valve.is_connected is False


async def test_setup_completes_on_a_valve_silent_on_optional_reads(
    hass: HomeAssistant, config_entry: MockConfigEntry, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    valve.ignore.update({Opcode.READ_USAGE, Opcode.READ_OUTLET_CONFIG})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.bathroom_last_run_volume").state == "unknown"


async def test_outlet_limits_changed_in_the_app_are_picked_up(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """The cap was once frozen at pairing; a maximum lowered for a child was ignored."""
    lowered = bytearray(OUTLET_1_CONFIG)
    lowered[3:5] = (380).to_bytes(2, "big")
    valve.outlet_configs[1] = bytes(lowered)
    await _tick(hass, IDLE_INTERVAL + 1)
    assert setup_entry.runtime_data.outlets[1].max_temperature == 45.0  # not re-read yet
    setup_entry.runtime_data._outlets_read_at -= timedelta(seconds=OUTLET_REFRESH)
    await _tick(hass, IDLE_INTERVAL + 1)
    assert setup_entry.runtime_data.outlets[1].max_temperature == 38.0
    assert hass.states.get("sensor.bathroom_outlet_1_maximum_temperature").state == "38.0"
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.bathroom_target_temperature", "value": 44},
        blocking=True,
    )
    await hass.services.async_call("switch", "turn_on", {"entity_id": HAND}, blocking=True)
    assert valve.target_tenths == 380


# --- pairing refused --------------------------------------------------------------------------


@contextlib.contextmanager
def _refuse(valve: FakeValve, *, refused: bool = True) -> Iterator[None]:
    valve.connect_error = PairingRequired(
        "Bathroom: Pairing failed due to error: 82", 82 if refused else 5, refused=refused
    )
    try:
        yield
    finally:
        valve.connect_error = None


def _reauth_flows(hass: HomeAssistant) -> list:
    return hass.config_entries.flow.async_progress_by_handler(
        DOMAIN, match_context={"source": SOURCE_REAUTH}
    )


def _issues(hass: HomeAssistant) -> set[str]:
    return {
        issue.translation_key or ""
        for issue in ir.async_get(hass).issues.values()
        if issue.domain == DOMAIN
    }


@pytest.fixture
def old_pairing(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    """The entry was paired long ago, so a refusal is about the bond, not a forgetful proxy."""
    hass.config_entries.async_update_entry(
        setup_entry, data={**setup_entry.data, CONF_PAIRED_AT: OLD}
    )


async def test_repeated_refusals_through_the_bonded_path_ask_for_pairing_again(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve, old_pairing: None
) -> None:
    coordinator = setup_entry.runtime_data
    with _refuse(valve):
        for attempt in range(1, MAX_PAIRING_REFUSALS):
            await _tick(hass, IDLE_INTERVAL + 1)
            assert coordinator.pairing_refusals == attempt
            assert not _reauth_flows(hass)
        await _tick(hass, IDLE_INTERVAL + 1)
        assert len(_reauth_flows(hass)) == 1
        assert coordinator.reauth_raised
        assert _interval(setup_entry) == REAUTH_INTERVAL
        await _tick(hass, REAUTH_INTERVAL + 1)
        assert len(_reauth_flows(hass)) == 1  # no second flow, no speeding back up
        assert _interval(setup_entry) == REAUTH_INTERVAL
    # The bond works after all: the open flow is closed, since pairing again would only move it.
    await _tick(hass, REAUTH_INTERVAL + 1)
    assert not _reauth_flows(hass)
    assert not coordinator.reauth_raised
    assert coordinator.pairing_refusals == 0
    assert hass.states.get(WATER).state == "25.6"


async def test_a_reauth_flow_left_over_from_before_a_reload_is_closed_on_recovery(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    await setup_entry.start_reauth_flow(hass)
    assert len(_reauth_flows(hass)) == 1
    assert not setup_entry.runtime_data.reauth_raised  # this coordinator never raised it
    await _tick(hass, IDLE_INTERVAL + 1)
    assert not _reauth_flows(hass)


async def test_a_subscribe_refused_for_want_of_encryption_never_asks_for_pairing(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve, old_pairing: None
) -> None:
    """A proxy with a stuck bond store looks exactly like this; pairing again is wrong."""
    with _refuse(valve, refused=False):
        for _ in range(MAX_PAIRING_REFUSALS + 2):
            await _tick(hass, IDLE_INTERVAL + 1)
    assert setup_entry.runtime_data.pairing_refusals == 0
    assert not _reauth_flows(hass)


async def test_a_refusal_between_good_polls_does_not_count_up(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve, old_pairing: None
) -> None:
    coordinator = setup_entry.runtime_data
    with _refuse(valve):
        await _tick(hass, IDLE_INTERVAL + 1)
        await _tick(hass, IDLE_INTERVAL + 1)
    await _tick(hass, IDLE_INTERVAL + 1)
    assert coordinator.pairing_refusals == 0
    valve.silent = True  # a radio failure also resets the count
    with _refuse(valve):
        await _tick(hass, IDLE_INTERVAL + 1)
    await _tick(hass, IDLE_INTERVAL + 1)
    assert coordinator.pairing_refusals == 0
    assert not _reauth_flows(hass)


async def test_refusals_through_an_adapter_that_never_held_the_bond_do_not_count(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve, old_pairing: None
) -> None:
    """Home Assistant picks the adapter per connection; only the bonded one can speak for
    the bond."""
    valve.source = "00:11:22:33:BB:02"  # the attempts went through another proxy
    with _refuse(valve):
        for _ in range(MAX_PAIRING_REFUSALS + 1):
            await _tick(hass, IDLE_INTERVAL + 1)
    assert setup_entry.runtime_data.pairing_refusals == 0
    assert not _reauth_flows(hass)
    assert setup_entry.runtime_data.last_source == "00:11:22:33:BB:02"


async def test_an_unknown_bonded_path_counts_every_refusal(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve, old_pairing: None
) -> None:
    hass.config_entries.async_update_entry(
        setup_entry, data={**setup_entry.data, CONF_PAIRED_SOURCE: "unknown"}
    )
    await hass.async_block_till_done()
    valve.source = "00:11:22:33:BB:02"
    with _refuse(valve):
        for _ in range(MAX_PAIRING_REFUSALS):
            await _tick(hass, IDLE_INTERVAL + 1)
    assert len(_reauth_flows(hass)) == 1


async def test_a_refusal_soon_after_pairing_blames_the_proxy_not_the_bond(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    """ESP-IDF 5.5.6 proxies forget a bond after one session; pairing again cannot help."""
    hass.config_entries.async_update_entry(
        setup_entry, data={**setup_entry.data, CONF_PAIRED_AT: dt_util.utcnow().isoformat()}
    )
    with _refuse(valve):
        for _ in range(MAX_PAIRING_REFUSALS + 2):
            await _tick(hass, IDLE_INTERVAL + 1)
        assert not _reauth_flows(hass)
        assert _issues(hass) == {"proxy_forgets_bond"}
    await _tick(hass, IDLE_INTERVAL + 1)
    assert _issues(hass) == set()  # cleared once the valve answers


async def test_a_refusal_at_startup_asks_for_pairing_at_once(
    hass: HomeAssistant, config_entry: MockConfigEntry, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    """Every setup retry builds a new coordinator, so a count would never reach three."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, data={**config_entry.data, CONF_PAIRED_AT: OLD}
    )
    with _refuse(valve):
        assert not await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert len(_reauth_flows(hass)) == 1


async def test_a_refusal_at_startup_soon_after_pairing_blames_the_proxy(
    hass: HomeAssistant, config_entry: MockConfigEntry, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    """Pairing succeeded (one session), and the very next connection is refused."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, data={**config_entry.data, CONF_PAIRED_AT: dt_util.utcnow().isoformat()}
    )
    with _refuse(valve):
        assert not await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert not _reauth_flows(hass)
    assert _issues(hass) == {"proxy_forgets_bond"}


async def test_weak_evidence_at_startup_only_retries(
    hass: HomeAssistant, config_entry: MockConfigEntry, proxy: FakeProxyScanner, valve: FakeValve
) -> None:
    config_entry.add_to_hass(hass)
    with _refuse(valve, refused=False):
        assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert not _reauth_flows(hass)


async def test_an_hour_of_failures_while_heard_raises_a_repair_that_does_not_ask_to_pair(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve, proxy: FakeProxyScanner
) -> None:
    """A wedged proxy once produced a week of this; the cure was a proxy restart."""
    valve.silent = True
    await _tick(hass, IDLE_INTERVAL + 1)
    assert _issues(hass) == set()
    with patch(
        "custom_components.mira_ble.coordinator.dt_util.utcnow",
        return_value=dt_util.utcnow() + timedelta(seconds=OUTAGE_ISSUE_AFTER + 60),
    ):
        await setup_entry.runtime_data.async_refresh()
    assert _issues(hass) == {"not_responding"}
    assert not _reauth_flows(hass)
    valve.silent = False
    await setup_entry.runtime_data.async_refresh()
    assert _issues(hass) == set()


async def test_no_repair_is_raised_for_a_valve_that_is_simply_not_there(
    hass: HomeAssistant, setup_entry: MockConfigEntry, valve: FakeValve
) -> None:
    with (
        patch(
            "custom_components.mira_ble.coordinator.bluetooth.async_ble_device_from_address",
            return_value=None,
        ),
        patch(
            "custom_components.mira_ble.coordinator.dt_util.utcnow",
            side_effect=[dt_util.utcnow(), dt_util.utcnow() + timedelta(hours=2)] * 4,
        ),
    ):
        await setup_entry.runtime_data.async_refresh()
        await setup_entry.runtime_data.async_refresh()
    assert _issues(hass) == set()
    assert PROXY_SOURCE  # the proxy fixture is what makes the valve heard in other tests
