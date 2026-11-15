"""Base entities."""

from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import MiraCoordinator


class MiraEntity(CoordinatorEntity[MiraCoordinator]):
    """An entity showing a reading; unavailable once several polls in a row have failed."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MiraCoordinator, key: str) -> None:
        """Bind the entity to its coordinator and give it a stable unique ID."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.unique_base}_{key}"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        """Ride out one or two missed polls on the last reading."""
        return self.coordinator.readings_fresh


class MiraControlEntity(MiraEntity):
    """A control that must stay usable when readings are stale: Stop above all.

    Home Assistant silently skips an action aimed at an unavailable entity, and a control
    opens its own connection anyway, so it does not depend on the last poll.
    """

    @property
    def available(self) -> bool:
        """Controls are always available."""
        return True
