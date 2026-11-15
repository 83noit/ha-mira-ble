"""Running and fault binary sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from mira_ble import State

from .coordinator import MiraConfigEntry, MiraCoordinator
from .entity import MiraEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class MiraBinarySensorDescription(BinarySensorEntityDescription):
    """A binary sensor read from the valve's state."""

    value_fn: Callable[[State], bool]


BINARY_SENSORS: tuple[MiraBinarySensorDescription, ...] = (
    MiraBinarySensorDescription(
        key="running",
        translation_key="running",
        device_class=BinarySensorDeviceClass.RUNNING,
        # Anything short of fully at rest: an open outlet, or a run the valve is holding
        # (which is how a preset shows on some models).
        value_fn=lambda state: state.active,
    ),
    MiraBinarySensorDescription(
        key="fault",
        translation_key="fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state.fault,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: MiraConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the binary sensors."""
    async_add_entities(
        MiraBinarySensor(entry.runtime_data, description) for description in BINARY_SENSORS
    )


class MiraBinarySensor(MiraEntity, BinarySensorEntity):
    """A binary sensor of one valve."""

    entity_description: MiraBinarySensorDescription

    def __init__(
        self, coordinator: MiraCoordinator, description: MiraBinarySensorDescription
    ) -> None:
        """Bind the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        """Return the sensor's state."""
        return self.entity_description.value_fn(self.coordinator.data.state)
