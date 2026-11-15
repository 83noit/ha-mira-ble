"""Config flow: discover the valve, bond with it while it is in pairing mode, read what it has.

The valve only talks over a bonded link and only accepts a new bond while it is in pairing
mode. The ``pair`` progress step connects (which pairs), then uses the same connection to
read the state, the identity, the outlets and the presets. The same step serves
reauthentication (the bond was lost) and reconfiguration (pair again on demand, for instance
after moving a proxy).

A valve is identified by its serial number, not its Bluetooth address: the address can change
across a power cut. A known valve that turns up at a new address is offered for pairing again
(its bond does not follow the address), and its entry moves only once the serial number has
been read over the bonded link; an advertisement alone never repoints an entry.

Nothing is destroyed on suspicion. The existing bond is tried first and removed only after
the valve refuses it, and a re-read that hit errors keeps the stored outlets and presets.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
import logging
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.util import dt as dt_util
import probatio

from mira_ble import (
    LEGACY_SERVICE_UUID,
    NO_ANSWER,
    BleTransport,
    Capabilities,
    Identity,
    MiraError,
    MiraValve,
    PairingRequired,
    parse_local_name,
    remove_bluez_bond,
)

from .const import (
    CONF_OUTLETS,
    CONF_PAIRED_AT,
    CONF_PAIRED_SOURCE,
    CONF_PRESETS,
    CONF_PRESETS_UNREADABLE,
    CONF_PRODUCT_CODE,
    CONF_READ_ERRORS,
    CONF_SERIAL_NUMBER,
    CONF_VALVE_NAME,
    DOMAIN,
    PAIR_TIMEOUT,
)
from .coordinator import OutletInfo

_LOGGER = logging.getLogger(__name__)

MOVED_NOTE = (
    "This valve is already set up, at another Bluetooth address: it has probably been "
    "power-cycled. Pairing it again moves its existing entry here.\n\n"
)


@dataclass(slots=True)
class PairOutcome:
    """What the pairing task found out."""

    error: str | None = None
    placeholders: dict[str, str] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    title: str | None = None
    serial_number: str | None = None


def _is_mira(service_info: BluetoothServiceInfoBleak) -> bool:
    """Return True for a valve of the supported generation.

    The name decides: other devices advertise the same service UUID, and a valve does not
    always advertise it. A valve of the older generation is refused whatever its name.
    """
    return (
        parse_local_name(service_info.name) is not None
        and LEGACY_SERVICE_UUID not in service_info.service_uuids
    )


def _connectable_scanners(hass: HomeAssistant, address: str) -> list[str]:
    return [
        f"{device.scanner.name} ({device.advertisement.rssi} dBm)"
        for device in bluetooth.async_scanner_devices_by_address(hass, address, connectable=True)
    ]


def _likely_path(hass: HomeAssistant, address: str) -> str:
    """Return the adapter or proxy most likely to be used: the strongest connectable one."""
    devices = bluetooth.async_scanner_devices_by_address(hass, address, connectable=True)
    if not devices:
        return "unknown"
    best = max(devices, key=lambda device: device.advertisement.rssi or -127)
    return str(best.scanner.source)


def entry_data(
    address: str,
    name: str | None,
    identity: Identity,
    capabilities: Capabilities,
    source: str,
) -> dict[str, Any]:
    """Build the config entry data from what pairing read off the valve."""
    advertisement = parse_local_name(name)
    preset_errors = {v for k, v in capabilities.errors.items() if k.startswith("preset")}
    return {
        CONF_ADDRESS: address,
        CONF_SERIAL_NUMBER: identity.serial_number,
        CONF_VALVE_NAME: identity.name,
        CONF_PRODUCT_CODE: advertisement.product_code if advertisement else None,
        CONF_OUTLETS: [OutletInfo.from_config(outlet).as_dict() for outlet in capabilities.outlets],
        CONF_PRESETS: [
            {"slot": preset.slot, "name": preset.name} for preset in capabilities.presets
        ],
        # A valve that stays silent on preset reads (a Platinum at rest) cannot have its
        # presets listed or checked; the integration then offers the slots unchecked.
        CONF_PRESETS_UNREADABLE: not capabilities.presets and NO_ANSWER in preset_errors,
        CONF_READ_ERRORS: dict(capabilities.errors),
        CONF_PAIRED_SOURCE: source,
        CONF_PAIRED_AT: dt_util.utcnow().isoformat(),
    }


def keep_on_error(entry: ConfigEntry, data: dict[str, Any]) -> dict[str, Any]:
    """Keep the stored outlets or presets when the re-read of that category hit an error.

    A read that failed is not evidence that an outlet or a preset is gone, and dropping them
    would delete their entities.
    """
    errors: dict[str, str] = data.get(CONF_READ_ERRORS, {})
    kept = dict(data)
    stored_outlets = entry.data.get(CONF_OUTLETS, [])
    stored_presets = entry.data.get(CONF_PRESETS, [])
    if any(k.startswith("outlet") for k in errors) and len(data[CONF_OUTLETS]) < len(
        stored_outlets
    ):
        kept[CONF_OUTLETS] = stored_outlets
    if any(k.startswith("preset") for k in errors) and len(data[CONF_PRESETS]) < len(
        stored_presets
    ):
        kept[CONF_PRESETS] = stored_presets
        kept[CONF_PRESETS_UNREADABLE] = entry.data.get(CONF_PRESETS_UNREADABLE, False)
    return kept


class MiraConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle discovery, pairing, reauth and reconfigure."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise flow state."""
        self._address: str | None = None
        self._name: str | None = None
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}
        self._pair_task: asyncio.Task[PairOutcome] | None = None
        self._outcome: PairOutcome | None = None
        self._entry: ConfigEntry | None = None
        self._moved = False
        """True when discovery found a valve already set up, at another address."""

    # -- discovery -----------------------------------------------------------------------------

    def _entry_for_serial(self, serial: str) -> ConfigEntry | None:
        return next(
            (e for e in self._async_current_entries(include_ignore=False) if e.unique_id == serial),
            None,
        )

    def _abort_if_address_configured(self, address: str) -> None:
        for entry in self._async_current_entries(include_ignore=False):
            if entry.data.get(CONF_ADDRESS) == address:
                raise AbortFlow("already_configured")

    async def _async_identify(self, address: str, name: str | None) -> None:
        """Set the flow's unique ID from what the advertisement gives away.

        With the serial number in the name a known valve is recognised at a new address. Its
        entry is not moved on the strength of an advertisement, which anyone in range could
        send: the flow goes on to pairing, and the entry moves once the serial number has
        been read over the bonded link. Without a serial in the name the address stands in
        until pairing has read it.
        """
        self._abort_if_address_configured(address)
        advertisement = parse_local_name(name)
        serial = advertisement.serial_number if advertisement is not None else None
        if serial and serial.strip("0"):
            await self.async_set_unique_id(serial, raise_on_progress=False)
            self._moved = self._entry_for_serial(serial) is not None
        else:
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a valve found by the Bluetooth integration."""
        if not _is_mira(discovery_info):
            return self.async_abort(reason="not_supported")
        await self._async_identify(discovery_info.address, discovery_info.name)
        self._address = discovery_info.address
        self._name = discovery_info.name or discovery_info.address
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Let the user pick a discovered valve."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            info = self._discovered[address]
            await self._async_identify(address, info.name)
            self._address = address
            self._name = info.name or address
            self.context["title_placeholders"] = {"name": self._name}
            return await self.async_step_bluetooth_confirm()

        configured = {
            entry.data.get(CONF_ADDRESS)
            for entry in self._async_current_entries(include_ignore=False)
        }
        for info in bluetooth.async_discovered_service_info(self.hass, connectable=True):
            if info.address in configured or not _is_mira(info):
                continue
            self._discovered[info.address] = info
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_ADDRESS): probatio.In(
                        {
                            address: f"{info.name or address} ({address})"
                            for address, info in self._discovered.items()
                        }
                    )
                }
            ),
        )

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explain pairing mode; refuse to start without a connectable adapter or proxy."""
        assert self._address is not None
        errors: dict[str, str] = {}
        connectable = _connectable_scanners(self.hass, self._address)
        if not connectable:
            errors["base"] = "no_connectable_adapter"
        elif user_input is not None:
            return await self.async_step_pair()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            errors=errors,
            description_placeholders={
                "name": self._name or self._address,
                "connectable": ", ".join(connectable) or "none",
                "moved": MOVED_NOTE if self._moved else "",
            },
        )

    # -- pairing -------------------------------------------------------------------------------

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Connect, pair and read the valve, as a progress step."""
        assert self._address is not None
        if self._pair_task is None:
            self._pair_task = self.hass.async_create_task(
                self._async_pair(self._address), eager_start=False
            )
        if not self._pair_task.done():
            return self.async_show_progress(
                step_id="pair",
                progress_action="pair",
                progress_task=self._pair_task,
                description_placeholders={"name": self._name or self._address},
            )
        self._outcome = self._pair_task.result()
        self._pair_task = None
        return self.async_show_progress_done(next_step_id="pair_done")

    async def async_step_pair_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create or update the entry, or show why pairing failed."""
        assert self._outcome is not None
        outcome = self._outcome
        if outcome.error is not None:
            return self.async_show_form(
                step_id="pair_failed",
                errors={"base": outcome.error},
                description_placeholders={"name": self._name or "", **outcome.placeholders},
            )
        if self._entry is not None:
            if outcome.serial_number and self._entry.unique_id not in (
                outcome.serial_number,
                self._entry.data.get(CONF_ADDRESS),
            ):
                return self.async_abort(reason="wrong_device")
            reason = (
                "reauth_successful" if self.source == SOURCE_REAUTH else "reconfigure_successful"
            )
            return self.async_update_reload_and_abort(
                self._entry, data_updates=keep_on_error(self._entry, outcome.data), reason=reason
            )
        if outcome.serial_number:
            # The serial number is the lasting identity, now read over the bonded link. A
            # valve already set up under it moves here, with its new pairing, rather than
            # being added twice.
            await self.async_set_unique_id(outcome.serial_number, raise_on_progress=False)
            if (existing := self._entry_for_serial(outcome.serial_number)) is not None:
                return self.async_update_reload_and_abort(
                    existing, data_updates=keep_on_error(existing, outcome.data), reason="moved"
                )
        title = outcome.title or self._name or self._address or "Mira"
        return self.async_create_entry(title=title, data=outcome.data)

    async def async_step_pair_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Run the pairing step again at the user's request."""
        return await self.async_step_pair()

    async def _async_pair(self, address: str) -> PairOutcome:
        """Run the pairing task with polling held off; it reports through the outcome."""
        coordinator = None
        if self._entry is not None and self._entry.state is ConfigEntryState.LOADED:
            coordinator = self._entry.runtime_data
            coordinator.pause_polling()
            await coordinator.async_release_for_pairing()
        try:
            return await self._async_connect_and_read(address)
        except Exception:
            _LOGGER.exception("Unexpected error while pairing with %s", address)
            return PairOutcome(error="unknown")
        finally:
            if coordinator is not None:
                coordinator.resume_polling()

    async def _async_forget_local_bonds(self, address: str) -> bool:
        """Make local BlueZ adapters forget their bond with the valve; True if one did.

        Called only after the valve has refused the bond: BlueZ would otherwise go on
        presenting keys the valve no longer accepts and never pair afresh. Proxies are left
        alone: unpairing a proxy on suspicion can strand a valve that was working.
        """
        removed = False
        for scanner_device in bluetooth.async_scanner_devices_by_address(
            self.hass, address, connectable=True
        ):
            removed |= await remove_bluez_bond(scanner_device.ble_device)
        return removed

    async def _async_connect_and_read(self, address: str) -> PairOutcome:
        """Connect (which pairs), then read the state, identity, outlets and presets.

        The existing bond is tried first. Only if the valve refuses it does a local adapter
        forget it, and then the connection is tried once more.
        """
        outcome = await self._async_attempt(address)
        if outcome.error == "not_in_pairing_mode" and await self._async_forget_local_bonds(address):
            outcome = await self._async_attempt(address)
        return outcome

    async def _async_attempt(self, address: str) -> PairOutcome:
        device = bluetooth.async_ble_device_from_address(self.hass, address, connectable=True)
        if device is None:
            return PairOutcome(error="no_connectable_adapter")
        transport = BleTransport(device, name=self._name, expect_new_bond=True)
        valve = MiraValve(transport)
        identity_errors: dict[str, str] = {}

        def source() -> str:
            return transport.source or _likely_path(self.hass, address)

        try:
            async with asyncio.timeout(PAIR_TIMEOUT):
                await transport.connect()
                # The one read every valve must answer: it proves the link and the protocol.
                await valve.read_state()
                identity = await valve.read_identity(identity_errors)
                capabilities = await valve.probe()
        except PairingRequired as err:
            _LOGGER.debug("Pairing failed via %s: %s", source(), err)
            return PairOutcome(
                error="not_in_pairing_mode" if err.refused else "not_encrypted",
                placeholders={"source": source()},
            )
        except (MiraError, TimeoutError) as err:
            _LOGGER.debug("Pairing connection failed via %s: %s", source(), err)
            return PairOutcome(
                error="cannot_connect",
                placeholders={"source": source(), "reason": f"{type(err).__name__}: {err}"},
            )
        finally:
            await transport.disconnect()
        capabilities.errors.update(identity_errors)
        for key, reason in capabilities.errors.items():
            _LOGGER.debug("%s not read from %s: %s", key, address, reason)
        _LOGGER.info(
            "Paired with %s via %s: %d outlet(s), %d preset(s)",
            address,
            source(),
            len(capabilities.outlets),
            len(capabilities.presets),
        )
        advertisement = parse_local_name(self._name)
        return PairOutcome(
            title=identity.name or (advertisement.label if advertisement else None) or self._name,
            serial_number=identity.serial_number,
            data=entry_data(address, self._name, identity, capabilities, source()),
        )

    # -- reauth and reconfigure ----------------------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start pairing again after the valve refused the stored pairing."""
        self._entry = self._get_reauth_entry()
        self._address = self._entry.data[CONF_ADDRESS]
        self._name = self._entry.title
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Check the valve is in range, then run the pairing step."""
        return await self._async_step_repair("reauth_confirm", user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair again on demand (for example after moving the proxy)."""
        if self._entry is None:
            self._entry = self._get_reconfigure_entry()
            self._address = self._entry.data[CONF_ADDRESS]
            self._name = self._entry.title
        return await self._async_step_repair("reconfigure", user_input)

    async def _async_step_repair(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        assert self._address is not None
        errors: dict[str, str] = {}
        if bluetooth.async_last_service_info(self.hass, self._address, connectable=False) is None:
            errors["base"] = "no_longer_in_range"
        elif not _connectable_scanners(self.hass, self._address):
            errors["base"] = "no_connectable_adapter"
        elif user_input is not None:
            return await self.async_step_pair()
        return self.async_show_form(
            step_id=step_id,
            errors=errors,
            description_placeholders={
                "name": self._name or self._address,
                "connectable": ", ".join(_connectable_scanners(self.hass, self._address)) or "none",
            },
        )
