"""Polling coordinator: one short connection per poll when idle, a held link while water runs.

The valve accepts one Bluetooth connection at a time, shared with the vendor app. Holding it
permanently would lock the app out, and connecting for every reading of a running shower
would be slow, so the link follows the water: it is opened for a poll or a command, kept
while the valve is active (polled every few seconds), and dropped once the valve is at rest.

Rules the commands follow, each from a failure found in review:

* the valve's state is read on the same link before every command, and an outlet change on a
  running valve re-sends the temperature and flow *just read*, never a stored value;
* the two numbers are what a start from Home Assistant asks for, and nothing else;
* once a command has been written, whatever then goes wrong, the last state read is
  published, polling goes to the running rate and a refresh is requested;
* a start from rest that cannot be confirmed is followed by a stop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from functools import partial
import logging
from typing import Any

from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothReachabilityIntent
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from mira_ble import (
    ABSOLUTE_MAX_TEMPERATURE,
    BleTransport,
    CommandRejected,
    LastUsage,
    MiraError,
    MiraValve,
    NotConfirmed,
    Outlet,
    OutletConfig,
    PairingRequired,
    State,
    UnsafeValue,
)

from .const import (
    BOND_GRACE,
    COMMAND_TIMEOUT,
    CONF_OUTLETS,
    CONF_PAIRED_AT,
    CONF_PAIRED_SOURCE,
    CONF_PRESETS,
    CONF_PRESETS_UNREADABLE,
    CONF_PRODUCT_CODE,
    CONF_SERIAL_NUMBER,
    DOMAIN,
    FAILURES_BEFORE_UNAVAILABLE,
    FALLBACK_FLOW,
    FALLBACK_MAX_FLOW,
    FALLBACK_MAX_TEMPERATURE,
    FALLBACK_MIN_FLOW,
    FALLBACK_MIN_TEMPERATURE,
    FALLBACK_TEMPERATURE,
    IDLE_INTERVAL,
    MANUFACTURER,
    MAX_OPTIONAL_BACKOFF,
    MAX_PAIRING_REFUSALS,
    OUTAGE_ISSUE_AFTER,
    OUTLET_REFRESH,
    POLL_TIMEOUT,
    REAUTH_INTERVAL,
    RUNNING_INTERVAL,
    RUNNING_RETRIES,
    STOP_ATTEMPTS,
    THROUGH_TIMEOUT,
    UNVERIFIED_PRESET_SLOTS,
)

type MiraConfigEntry = ConfigEntry[MiraCoordinator]

_LOGGER = logging.getLogger(__name__)

ISSUE_NOT_RESPONDING = "not_responding"
ISSUE_PROXY_FORGETS_BOND = "proxy_forgets_bond"


@dataclass(frozen=True, slots=True)
class OutletInfo:
    """An outlet's limits: stored in the config entry at pairing, refreshed while running."""

    number: int
    kind: str
    min_temperature: float
    max_temperature: float
    default_temperature: float
    min_flow: float
    max_flow: float
    default_flow: float
    max_duration: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OutletInfo:
        """Rebuild from the entry data."""
        return cls(**{f.name: data[f.name] for f in fields(cls) if f.name in data})

    @classmethod
    def from_config(cls, config: OutletConfig) -> OutletInfo:
        """Build from an outlet configuration read off the valve."""
        return cls(
            number=config.number,
            kind=config.kind.name.lower(),
            min_temperature=config.min_temperature,
            max_temperature=config.max_temperature,
            default_temperature=config.default_temperature,
            min_flow=config.min_flow,
            max_flow=config.max_flow,
            default_flow=config.default_flow,
            max_duration=config.max_duration,
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the form stored in the entry data."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True, slots=True)
class ValveData:
    """What one poll produced."""

    state: State
    last_usage: LastUsage | None = None


@dataclass(slots=True)
class _Attempt:
    """What a command got up to, for the clean-up after a failure."""

    written: bool = False
    """True from just before the first command frame goes out."""
    from_rest: bool = False
    """True when the command starts water on a valve that was at rest."""
    state: State | None = None
    """The last state read on the link."""


class MiraCoordinator(DataUpdateCoordinator[ValveData]):
    """Polls one valve and carries out commands on the same link."""

    config_entry: MiraConfigEntry

    def __init__(self, hass: HomeAssistant, entry: MiraConfigEntry) -> None:
        """Set up the coordinator for ``entry``."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=entry.title,
            update_interval=timedelta(seconds=IDLE_INTERVAL),
        )
        self.address: str = entry.data[CONF_ADDRESS]
        self.unique_base: str = entry.unique_id or self.address
        self.outlets: dict[int, OutletInfo] = {
            info.number: info
            for info in (OutletInfo.from_dict(o) for o in entry.data.get(CONF_OUTLETS, ()))
        }
        self.presets_verifiable = not entry.data.get(CONF_PRESETS_UNREADABLE, False)
        """False when the valve did not answer preset reads at pairing."""
        self.presets: dict[int, str | None] = {
            int(preset["slot"]): str(preset["name"]) for preset in entry.data.get(CONF_PRESETS, ())
        }
        if not self.presets and not self.presets_verifiable:
            self.presets = dict.fromkeys(UNVERIFIED_PRESET_SLOTS)
        first = next(iter(self.outlets.values()), None)
        self.temperature_setpoint: float = (
            first.default_temperature if first else FALLBACK_TEMPERATURE
        )
        """What the next start from Home Assistant asks for; never taken from the valve."""
        self.flow_setpoint: float = first.default_flow if first else FALLBACK_FLOW
        self.poll_count = 0
        self.consecutive_failures = 0
        self.pairing_refusals = 0
        self.reauth_raised = False
        self.last_error: str | None = None
        self.last_source: str | None = None
        self.last_frames: list[tuple[str, str]] = []
        self.optional_skips: dict[str, int] = {}
        """Polls left before each optional read that went unanswered is tried again."""
        self._optional_backoff: dict[str, int] = {}
        self._failing_since: datetime | None = None
        self._was_active = False
        self._failures_while_active = 0
        self._through: tuple[int, float] | None = None
        """A preset to start again once its warm-up ends in a hold, and until when."""
        self._outlets_read_at: datetime | None = None
        self._transport: BleTransport | None = None
        self._lock = asyncio.Lock()
        self._paused = False
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, self.unique_base)},
            connections={(CONNECTION_BLUETOOTH, self.address)},
            manufacturer=MANUFACTURER,
            name=entry.title,
            model_id=entry.data.get(CONF_PRODUCT_CODE),
            serial_number=entry.data.get(CONF_SERIAL_NUMBER),
        )

    # -- limits and setpoints ----------------------------------------------------------------

    @property
    def min_temperature(self) -> float:
        """The lowest target any outlet accepts."""
        return min(
            (o.min_temperature for o in self.outlets.values()), default=FALLBACK_MIN_TEMPERATURE
        )

    @property
    def max_temperature(self) -> float:
        """The highest target any outlet accepts; each outlet's own maximum still applies."""
        return min(
            max(
                (o.max_temperature for o in self.outlets.values()),
                default=FALLBACK_MAX_TEMPERATURE,
            ),
            ABSOLUTE_MAX_TEMPERATURE,
        )

    @property
    def min_flow(self) -> float:
        """The lowest flow any outlet accepts."""
        return min((o.min_flow for o in self.outlets.values()), default=FALLBACK_MIN_FLOW)

    @property
    def max_flow(self) -> float:
        """The highest flow any outlet accepts."""
        return max((o.max_flow for o in self.outlets.values()), default=FALLBACK_MAX_FLOW)

    def _known(self, outlets: Outlet) -> list[OutletInfo]:
        return [self.outlets[n] for n in outlets.numbers if n in self.outlets]

    def temperature_limit(self, outlets: Outlet) -> float:
        """Return the lowest maximum temperature among ``outlets`` that are known."""
        return min(
            (o.max_temperature for o in self._known(outlets)), default=FALLBACK_MAX_TEMPERATURE
        )

    def clamp_flow(self, flow: float, outlets: Outlet) -> float:
        """Return ``flow`` brought inside the range every one of ``outlets`` accepts."""
        known = self._known(outlets)
        low = max((o.min_flow for o in known), default=FALLBACK_MIN_FLOW)
        high = min((o.max_flow for o in known), default=FALLBACK_MAX_FLOW)
        return max(min(flow, high), min(low, high))

    @property
    def readings_fresh(self) -> bool:
        """True while there is a reading and recent polls have not all failed.

        One or two missed polls are noise on a radio link; treating them as an outage would
        blank every entity and reset the timers of automations watching them.
        """
        return self.data is not None and self.consecutive_failures < FAILURES_BEFORE_UNAVAILABLE

    @property
    def target_temperature(self) -> float | None:
        """The valve's target while water runs under an outlet; otherwise the next start's."""
        state = self.data.state if self.data else None
        if state is not None and state.outlets:
            return state.target_temperature  # None is "cold"
        return self.temperature_setpoint

    @property
    def target_flow(self) -> float:
        """The valve's flow setting while water runs under an outlet; otherwise the next start's."""
        state = self.data.state if self.data else None
        if state is not None and state.outlets:
            return state.flow_setting
        return self.flow_setpoint

    # -- connection --------------------------------------------------------------------------

    def reachability(self) -> str:
        """Home Assistant's account of the paths to the valve, for logs and diagnostics."""
        return bluetooth.async_address_reachability_diagnostics(
            self.hass, self.address.upper(), BluetoothReachabilityIntent.CONNECTION
        )

    def _connectable(self) -> bool:
        return (
            bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
            is not None
        )

    async def _async_connected_valve(self) -> MiraValve:
        """Return a client on an open link, connecting if there is none."""
        if self._transport is None or not self._transport.is_connected:
            device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if device is None:
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="not_reachable",
                    translation_placeholders={"name": self.name},
                )
            self._transport = BleTransport(
                device,
                name=self.name,
                device_callback=lambda: bluetooth.async_ble_device_from_address(
                    self.hass, self.address, connectable=True
                ),
            )
            try:
                await self._transport.connect()
            finally:
                self.last_source = self._transport.source
        # The outlets' own maxima are applied per command; this is only the outer bound.
        return MiraValve(self._transport, max_temperature=ABSOLUTE_MAX_TEMPERATURE)

    async def _async_release(self, *, keep: bool) -> None:
        """Record the link's traffic and drop it unless the valve is active."""
        transport = self._transport
        if transport is None:
            return
        self.last_frames = [
            (direction, frame.hex(" ")) for direction, frame in transport.last_frames
        ]
        if not keep or not transport.is_connected:
            self._transport = None
            await transport.disconnect()

    async def async_shutdown(self) -> None:
        """Stop polling and close the link. Water that is running is left running."""
        await super().async_shutdown()
        async with self._lock:
            await self._async_release(keep=False)

    @callback
    def pause_polling(self) -> None:
        """Hold polling and commands while a pairing flow owns the valve's one connection."""
        self._paused = True

    @callback
    def resume_polling(self) -> None:
        """Let polling and commands continue."""
        self._paused = False

    async def async_release_for_pairing(self) -> None:
        """Close the link so that a pairing flow can connect."""
        async with self._lock:
            await self._async_release(keep=False)

    # -- polling -----------------------------------------------------------------------------

    async def _async_update_data(self) -> ValveData:
        if self._paused:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="pairing_in_progress",
                translation_placeholders={"name": self.name},
            )
        self.poll_count += 1
        keep = False
        async with self._lock:
            try:
                async with asyncio.timeout(POLL_TIMEOUT):
                    valve = await self._async_connected_valve()
                    state = await valve.read_state()
                    state = await self._async_continue_preset(valve, state)
                    keep = state.active
                    last_usage = self.data.last_usage if self.data else None
                    if not state.active:
                        last_usage = await self._async_idle_reads(valve, last_usage)
            except UpdateFailed as err:
                self._note_failure(err)
                raise
            except PairingRequired as err:
                self._note_pairing_failure(err)
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="pairing_required" if err.refused else "not_encrypted",
                    translation_placeholders={"name": self.name},
                ) from err
            except (MiraError, BleakError, TimeoutError) as err:
                self.pairing_refusals = 0
                self._note_failure(err)
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="poll_failed",
                    translation_placeholders={"name": self.name, "error": self.last_error or ""},
                ) from err
            finally:
                await self._async_release(keep=keep)
        self._note_success(state)
        return ValveData(state=state, last_usage=last_usage)

    async def _async_idle_reads(
        self, valve: MiraValve, last_usage: LastUsage | None
    ) -> LastUsage | None:
        """Read what is only worth reading at rest; none of it may fail the poll."""
        usage = await self._async_optional("last_usage", valve.read_last_usage)
        if usage is not None:
            last_usage = usage
        now = dt_util.utcnow()
        due = self._outlets_read_at is None or (
            now - self._outlets_read_at >= timedelta(seconds=OUTLET_REFRESH)
        )
        if due and self.outlets:
            self._outlets_read_at = now
            for number in tuple(self.outlets):
                config = await self._async_optional(
                    f"outlet_{number}", partial(valve.read_outlet_config, number)
                )
                if config is not None and config.max_temperature > 0:
                    self.outlets[number] = OutletInfo.from_config(config)
        return last_usage

    async def _async_optional[T](self, key: str, read: Callable[[], Awaitable[T]]) -> T | None:
        """Run a read the valve may ignore or refuse; back off when it does.

        A valve that does not support a read may stay silent, which costs a timeout and the
        link. So an unanswered read is skipped for 1, 2, 4 ... polls before it is asked again.
        """
        if self.optional_skips.get(key, 0) > 0:
            self.optional_skips[key] -= 1
            return None
        transport = self._transport
        if transport is None or not transport.is_connected:
            return None  # an earlier optional read cost the link; the state is already in hand
        try:
            value = await read()
        except MiraError as err:
            backoff = min(max(self._optional_backoff.get(key, 0) * 2, 1), MAX_OPTIONAL_BACKOFF)
            self._optional_backoff[key] = self.optional_skips[key] = backoff
            _LOGGER.debug(
                "%s: %s not read (%s); skipping it for %d poll(s)", self.name, key, err, backoff
            )
            return None
        self._optional_backoff.pop(key, None)
        self.optional_skips.pop(key, None)
        return value

    def _note_failure(self, err: BaseException) -> None:
        self.consecutive_failures += 1
        self.last_error = f"{type(err).__name__}: {err}"
        now = dt_util.utcnow()
        if self._failing_since is None:
            self._failing_since = now
        if self._was_active:
            self._failures_while_active += 1
        if self.reauth_raised:
            interval = REAUTH_INTERVAL
        elif self._was_active and self._failures_while_active <= RUNNING_RETRIES:
            # Water was running at the last reading: keep looking, and keep Stop close.
            interval = RUNNING_INTERVAL
        else:
            interval = IDLE_INTERVAL
        self.update_interval = timedelta(seconds=interval)
        if self.consecutive_failures == FAILURES_BEFORE_UNAVAILABLE:
            # The base class told the entities about the first failure only; tell them again
            # now that the grace period is over.
            self.async_update_listeners()
        if (
            now - self._failing_since >= timedelta(seconds=OUTAGE_ISSUE_AFTER)
            and self._connectable()
        ):
            self._raise_issue(ISSUE_NOT_RESPONDING)

    def _raise_issue(self, key: str) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{key}_{self.config_entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=key,
            translation_placeholders={"name": self.name},
        )

    def _clear_issues(self) -> None:
        for key in (ISSUE_NOT_RESPONDING, ISSUE_PROXY_FORGETS_BOND):
            ir.async_delete_issue(self.hass, DOMAIN, f"{key}_{self.config_entry.entry_id}")

    def _through_bonded_path(self) -> bool:
        """Return True unless the last attempt is known to have used another adapter."""
        paired = self.config_entry.data.get(CONF_PAIRED_SOURCE)
        if not paired or paired == "unknown" or self.last_source is None:
            return True
        return bool(self.last_source == paired)

    def _recently_paired(self) -> bool:
        paired_at = dt_util.parse_datetime(str(self.config_entry.data.get(CONF_PAIRED_AT, "")))
        return paired_at is not None and (
            dt_util.utcnow() - paired_at < timedelta(seconds=BOND_GRACE)
        )

    def _note_pairing_failure(self, err: PairingRequired) -> None:
        """Decide what a link that could not be encrypted says about the bond.

        Only an outright refusal, through the adapter that holds the bond, counts toward
        asking for pairing again. A subscribe refused for want of encryption is weak evidence
        (a proxy with a stuck bond store looks the same), and a refusal through another
        adapter says nothing about the bond at all. A refusal soon after a pairing that
        worked points at a proxy that forgets bonds, where pairing again cannot help.
        """
        self._note_failure(err)
        if not err.refused or not self._through_bonded_path():
            self.pairing_refusals = 0
            return
        if self._recently_paired():
            self._raise_issue(ISSUE_PROXY_FORGETS_BOND)
            return
        self.pairing_refusals += 1
        if self.data is None:
            # The first poll of a setup: every retry starts a new count, so ask at once.
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="pairing_required",
                translation_placeholders={"name": self.name},
            ) from err
        if self.pairing_refusals >= MAX_PAIRING_REFUSALS and not self.reauth_raised:
            self.reauth_raised = True
            self.update_interval = timedelta(seconds=REAUTH_INTERVAL)
            _LOGGER.warning(
                "%s refused pairing %d times in a row; it needs pairing again",
                self.name,
                self.pairing_refusals,
            )
            self.config_entry.async_start_reauth(self.hass)

    def _note_success(self, state: State) -> None:
        self.last_error = None
        self.consecutive_failures = 0
        self.pairing_refusals = 0
        self._failing_since = None
        self._failures_while_active = 0
        self._was_active = state.active
        self.reauth_raised = False
        self._clear_issues()
        # A flow left over from before a reload is closed too: pairing again now would only
        # move a bond that works.
        for flow in self.hass.config_entries.flow.async_progress_by_handler(
            DOMAIN,
            match_context={"source": SOURCE_REAUTH, "entry_id": self.config_entry.entry_id},
        ):
            self.hass.config_entries.flow.async_abort(flow["flow_id"])
        self.update_interval = timedelta(
            seconds=RUNNING_INTERVAL if state.active else IDLE_INTERVAL
        )

    async def _async_continue_preset(self, valve: MiraValve, state: State) -> State:
        """Start the preset proper once a preset run with "run preset" has warmed up.

        Measured on a Mira Platinum: the first preset command warms the water up and the
        valve holds; the same command during the hold runs the preset with its timer.
        Anything else the valve does meanwhile (rest, a run with no warm-up, a preset
        already counting down) ends the wait, and so does any other command.
        """
        if self._through is None:
            return state
        slot, deadline = self._through
        if state.held and not state.running:  # held, and the water has stopped
            try:
                started = await valve.run_preset(slot, before=state)
            except (NotConfirmed, CommandRejected) as err:
                self._through = None
                _LOGGER.warning(
                    "%s: preset %d did not start after its warm-up: %s", self.name, slot, err
                )
                return await valve.read_state()
            self._through = None
            assert started is not None
            return started
        if (
            not state.active
            # An open outlet with no warm-up flag: a preset running at once, or a plain run.
            # Not merely "running": the water drains for a moment as a warm-up ends.
            or (bool(state.outlets) and not state.warming_up)
            or self.hass.loop.time() > deadline
        ):
            self._through = None
        return state

    # -- commands ----------------------------------------------------------------------------

    def _publish(self, state: State) -> None:
        last_usage = self.data.last_usage if self.data else None
        self.async_set_updated_data(ValveData(state=state, last_usage=last_usage))

    async def _async_command(
        self, action: Callable[[MiraValve, State, _Attempt], Awaitable[State]]
    ) -> None:
        """Run ``action`` on an open link with the valve's present state, then publish the result.

        The state is read on the same link first, so a command never builds on a reading that
        is a poll interval old (someone may have changed the shower at the wall meanwhile).
        """
        if self._paused:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="pairing_in_progress",
                translation_placeholders={"name": self.name},
            )
        attempt = _Attempt()
        done = False
        self._through = None  # any command ends the wait to continue a preset
        async with self._lock:
            try:
                async with asyncio.timeout(COMMAND_TIMEOUT):
                    try:
                        valve = await self._async_connected_valve()
                        attempt.state = await valve.read_state()
                        attempt.state = await action(valve, attempt.state, attempt)
                        done = True
                    except (MiraError, BleakError, UpdateFailed) as err:
                        raise await self._async_failed(err, attempt) from err
            except TimeoutError as err:
                raise await self._async_failed(err, attempt) from err
            finally:
                state = attempt.state
                await self._async_release(keep=state is not None and state.active)
                if not done and attempt.written:
                    self._after_uncertain_command(state)
        assert attempt.state is not None
        self._note_success(attempt.state)
        self._publish(attempt.state)

    def _after_uncertain_command(self, state: State | None) -> None:
        """Tell the truth after a command that was written but did not complete.

        Whatever the valve last reported is shown, polling goes to the running rate, and a
        fresh reading is asked for at once: the water may be running.
        """
        if state is not None:
            self._publish(state)
        self._was_active = True
        self.update_interval = timedelta(seconds=RUNNING_INTERVAL)
        self.hass.async_create_task(self.async_request_refresh())

    async def _async_failed(self, err: BaseException, attempt: _Attempt) -> HomeAssistantError:
        """Turn a failed command into the error to raise, stopping an unconfirmed start."""
        if isinstance(err, UnsafeValue):
            return ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unsafe_value",
                translation_placeholders={"error": str(err)},
            )
        if isinstance(err, UpdateFailed):
            return HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="not_reachable",
                translation_placeholders={"name": self.name},
            )
        if isinstance(err, NotConfirmed) and err.state is not None:
            attempt.state = err.state
        detail = f"{type(err).__name__}: {err}" if str(err) else type(err).__name__
        key = "not_confirmed" if isinstance(err, NotConfirmed) else "command_failed"
        if attempt.written and attempt.from_rest:
            # Water may have started with nothing confirming it: stop it, do not just report.
            key = (
                "start_stopped"
                if await self._async_precautionary_stop(attempt)
                else "start_unknown"
            )
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders={"name": self.name, "error": detail},
        )

    async def _async_precautionary_stop(self, attempt: _Attempt) -> bool:
        """Stop a valve whose start could not be confirmed; True when the stop was confirmed."""
        try:
            valve = await self._async_connected_valve()
            state = await valve.stop()
        except NotConfirmed as err:
            attempt.state = err.state or attempt.state
            return False
        except (MiraError, BleakError, UpdateFailed, TimeoutError) as err:
            _LOGGER.warning("%s: could not stop after an unconfirmed start: %s", self.name, err)
            return False
        attempt.state = state
        return True

    async def async_set_outlet(
        self,
        number: int,
        *,
        on: bool,
        temperature: float | None = None,
        flow: float | None = None,
    ) -> None:
        """Open or close one outlet, leaving the others as the valve reports them.

        ``temperature`` and ``flow`` apply to this command only (the start action); they are
        checked against the outlet's own limits and are not stored.
        """
        bit = Outlet.from_number(number)
        if on:
            self._check_explicit(bit, temperature, flow)

        async def action(valve: MiraValve, current: State, attempt: _Attempt) -> State:
            wanted = Outlet((current.outlets | bit) if on else (current.outlets & ~bit))
            if on and wanted == current.outlets and temperature is None and flow is None:
                return current
            if not wanted:
                if not current.running:
                    return current
                attempt.written = True
                return await self._async_stop_on(valve)
            if not on and wanted == current.outlets:
                return current  # that outlet was not open; leave the others alone
            return await self._async_run(
                valve,
                current,
                wanted,
                attempt,
                added=bit if on else Outlet.NONE,
                temperature=temperature,
                flow=flow,
            )

        await self._async_command(action)

    def _check_explicit(
        self, outlets: Outlet, temperature: float | None, flow: float | None
    ) -> None:
        """Refuse a start action's values that the outlet does not allow, before connecting."""
        limit = self.temperature_limit(outlets)
        if temperature is not None and temperature > limit:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="temperature_above_outlet",
                translation_placeholders={"temperature": f"{temperature:g}", "limit": f"{limit:g}"},
            )
        if flow is not None and self.clamp_flow(flow, outlets) != flow:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="flow_outside_outlet",
                translation_placeholders={"flow": f"{flow:g}"},
            )

    async def _async_run(
        self,
        valve: MiraValve,
        current: State,
        wanted: Outlet,
        attempt: _Attempt,
        *,
        added: Outlet = Outlet.NONE,
        temperature: float | None = None,
        flow: float | None = None,
    ) -> State:
        """Send ``wanted``; what temperature and flow go with it depends on who chose them.

        Water already running under an outlet keeps the target and flow the valve reports
        (lowered, never raised, to a newly added outlet's maximum). A start from rest uses
        the numbers, capped and clamped to the started outlets' own limits.
        """
        limit: float | None
        if current.outlets:
            target = current.target_temperature if temperature is None else temperature
            send_flow = current.flow_setting if flow is None else self.clamp_flow(flow, wanted)
            limit = self.temperature_limit(added) if added and self._known(added) else None
            if temperature is not None:
                limit = self.temperature_limit(wanted)
            if target is not None and limit is not None:
                target = min(target, limit)
            if flow is None and send_flow <= 0:
                send_flow = self.clamp_flow(self.flow_setpoint, wanted)
        else:
            attempt.from_rest = not current.running
            limit = self.temperature_limit(wanted)
            target = min(self.temperature_setpoint if temperature is None else temperature, limit)
            send_flow = self.clamp_flow(self.flow_setpoint if flow is None else flow, wanted)
        attempt.written = True
        state = await valve.set_outlets(
            wanted, temperature=target, flow=send_flow, max_temperature=limit
        )
        assert state is not None
        return state

    @staticmethod
    async def _async_stop_on(valve: MiraValve) -> State:
        state = await valve.stop()
        assert state is not None
        return state

    async def async_stop(self) -> None:
        """Close every outlet, including a running preset; retried on fresh connections."""

        async def action(valve: MiraValve, _current: State, attempt: _Attempt) -> State:
            attempt.written = True
            return await self._async_stop_on(valve)

        for remaining in reversed(range(STOP_ATTEMPTS)):
            try:
                await self._async_command(action)
            except HomeAssistantError as err:
                if not remaining or self._paused:
                    raise
                _LOGGER.warning("%s: stop failed (%s); trying again", self.name, err)
            else:
                return

    async def async_run_preset(self, slot: int, *, through: bool = False) -> None:
        """Start a stored preset, after checking it is still the preset the button names.

        With ``through``, a preset that warms up and holds is started again at the hold by
        the next poll, so it runs with its timer and nobody has to press anything.
        """

        async def action(valve: MiraValve, current: State, attempt: _Attempt) -> State:
            if self.presets_verifiable:
                await self._async_verify_preset(valve, slot)
            attempt.from_rest = not current.running
            attempt.written = True
            state = await valve.run_preset(slot, before=current)
            assert state is not None
            return state

        await self._async_command(action)
        if through and self.data.state.warming_up:
            self._through = (slot, self.hass.loop.time() + THROUGH_TIMEOUT)

    async def _async_verify_preset(self, valve: MiraValve, slot: int) -> None:
        """Refuse a preset that is gone, renamed, or hotter than its outlets allow.

        Presets are edited in the vendor app; the button's name was read at pairing.
        """
        errors: dict[str, str] = {}
        preset = await valve.optional("preset", partial(valve.read_preset, slot), errors)
        expected = self.presets.get(slot)
        if errors or preset is None or (expected is not None and preset.name != expected):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="preset_changed",
                translation_placeholders={"slot": str(slot), "name": expected or str(slot)},
            )
        limit = self.temperature_limit(preset.outlets)
        if preset.temperature > limit:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="preset_too_hot",
                translation_placeholders={
                    "name": preset.name,
                    "temperature": f"{preset.temperature:g}",
                    "limit": f"{limit:g}",
                },
            )

    async def async_set_temperature(self, temperature: float) -> None:
        """Choose the target temperature; applied at once when water runs under an outlet."""
        await self._async_set_setpoint(temperature=temperature)

    async def async_set_flow(self, flow: float) -> None:
        """Choose the flow; applied at once when water runs under an outlet."""
        await self._async_set_setpoint(flow=flow)

    def _store_setpoint(self, temperature: float | None, flow: float | None) -> None:
        if temperature is not None:
            self.temperature_setpoint = temperature
        if flow is not None:
            self.flow_setpoint = flow

    async def _async_set_setpoint(
        self, *, temperature: float | None = None, flow: float | None = None
    ) -> None:
        state = self.data.state if self.data else None
        if state is None or not state.outlets:
            # Nothing to send: this is the value for the next start.
            self._store_setpoint(temperature, flow)
            self.async_update_listeners()
            return

        async def action(valve: MiraValve, current: State, attempt: _Attempt) -> State:
            if not current.outlets:
                return current  # it stopped at the wall meanwhile
            return await self._async_run(
                valve, current, current.outlets, attempt, temperature=temperature, flow=flow
            )

        await self._async_command(action)
        # Only a change the valve took is remembered; a failed one must not surface later.
        self._store_setpoint(temperature, flow)
        self.async_update_listeners()
