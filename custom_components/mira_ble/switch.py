"""Outlet switches."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import MiraConfigEntry, MiraCoordinator, OutletInfo
from .entity import MiraEntity

PARALLEL_UPDATES = 1
"""The valve takes one command at a time."""

NAMED_KINDS = {"hand_shower", "overhead", "bath_filler"}


async def async_setup_entry(
    hass: HomeAssistant, entry: MiraConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add a switch for each outlet the valve has."""
    coordinator = entry.runtime_data
    kinds = [outlet.kind for outlet in coordinator.outlets.values()]
    async_add_entities(
        MiraOutletSwitch(
            coordinator, outlet, named=outlet.kind in NAMED_KINDS and kinds.count(outlet.kind) == 1
        )
        for outlet in coordinator.outlets.values()
    )


class MiraOutletSwitch(MiraEntity, SwitchEntity):
    """One outlet: on while the valve reports it open."""

    def __init__(self, coordinator: MiraCoordinator, outlet: OutletInfo, *, named: bool) -> None:
        """Name the switch after what the outlet feeds when that is known and unambiguous."""
        super().__init__(coordinator, f"outlet_{outlet.number}")
        self._number = outlet.number
        if named:
            self._attr_translation_key = outlet.kind
        else:
            self._attr_translation_key = "outlet"
            self._attr_translation_placeholders = {"number": str(outlet.number)}

    @property
    def is_on(self) -> bool:
        """Return True while this outlet is reported open."""
        return self.coordinator.data.state.outlet_running(self._number)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Open the outlet at the temperature and flow the numbers show."""
        await self.coordinator.async_set_outlet(self._number, on=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Close the outlet; the valve stops when it was the last one open."""
        await self.coordinator.async_set_outlet(self._number, on=False)

    async def async_start(
        self, temperature: float | None = None, flow: float | None = None
    ) -> None:
        """Open the outlet at a temperature and flow given for this start only."""
        await self.coordinator.async_set_outlet(
            self._number, on=True, temperature=temperature, flow=flow
        )
