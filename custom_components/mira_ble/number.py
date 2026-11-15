"""Target temperature and flow."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import UnitOfTemperature, UnitOfVolumeFlowRate
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import MiraConfigEntry, MiraCoordinator
from .entity import MiraControlEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class MiraNumberDescription(NumberEntityDescription):
    """A number with its limits and its accessors on the coordinator."""

    value_fn: Callable[[MiraCoordinator], float | None]
    min_fn: Callable[[MiraCoordinator], float]
    max_fn: Callable[[MiraCoordinator], float]
    set_fn: Callable[[MiraCoordinator, float], Awaitable[None]]
    restore_fn: Callable[[MiraCoordinator, float], None]


def _restore_temperature(coordinator: MiraCoordinator, value: float) -> None:
    coordinator.temperature_setpoint = value


def _restore_flow(coordinator: MiraCoordinator, value: float) -> None:
    coordinator.flow_setpoint = value


NUMBERS: tuple[MiraNumberDescription, ...] = (
    MiraNumberDescription(
        key="target_temperature",
        translation_key="target_temperature",
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_step=0.5,
        mode=NumberMode.SLIDER,
        value_fn=lambda coordinator: coordinator.target_temperature,
        min_fn=lambda coordinator: coordinator.min_temperature,
        max_fn=lambda coordinator: coordinator.max_temperature,
        set_fn=lambda coordinator, value: coordinator.async_set_temperature(value),
        restore_fn=_restore_temperature,
    ),
    MiraNumberDescription(
        key="target_flow",
        translation_key="target_flow",
        device_class=NumberDeviceClass.VOLUME_FLOW_RATE,
        native_unit_of_measurement=UnitOfVolumeFlowRate.LITERS_PER_MINUTE,
        native_step=0.5,
        mode=NumberMode.SLIDER,
        value_fn=lambda coordinator: coordinator.target_flow,
        min_fn=lambda coordinator: coordinator.min_flow,
        max_fn=lambda coordinator: coordinator.max_flow,
        set_fn=lambda coordinator, value: coordinator.async_set_flow(value),
        restore_fn=_restore_flow,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: MiraConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the temperature and flow numbers."""
    async_add_entities(MiraNumber(entry.runtime_data, description) for description in NUMBERS)


class MiraNumber(MiraControlEntity, RestoreNumber):
    """The value the next start from Home Assistant uses; applied at once while water runs.

    While water runs under an outlet it shows what the valve reports. At rest it shows the
    stored value, which survives restarts and is never taken from the valve: a start from
    Home Assistant must not inherit whatever the last person used at the wall.
    """

    entity_description: MiraNumberDescription

    def __init__(self, coordinator: MiraCoordinator, description: MiraNumberDescription) -> None:
        """Bind the number."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_added_to_hass(self) -> None:
        """Bring back the value chosen before the restart, when it is still in range."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is None or last.native_value is None:
            return
        if self.native_min_value <= last.native_value <= self.native_max_value:
            self.entity_description.restore_fn(self.coordinator, last.native_value)

    @property
    def native_min_value(self) -> float:
        """The lowest value any outlet accepts, as the valve last reported its limits."""
        return self.entity_description.min_fn(self.coordinator)

    @property
    def native_max_value(self) -> float:
        """The highest value any outlet accepts; each outlet's own limit still applies."""
        return self.entity_description.max_fn(self.coordinator)

    @property
    def native_value(self) -> float | None:
        """Return the valve's value while it runs, else the value for the next start."""
        return self.entity_description.value_fn(self.coordinator)

    async def async_set_native_value(self, value: float) -> None:
        """Choose a new value."""
        await self.entity_description.set_fn(self.coordinator, value)
