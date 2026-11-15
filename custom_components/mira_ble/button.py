"""Stop and preset buttons."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import MiraConfigEntry, MiraCoordinator
from .entity import MiraControlEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: MiraConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the stop button and one button per preset."""
    coordinator = entry.runtime_data
    entities: list[ButtonEntity] = [MiraStopButton(coordinator)]
    entities.extend(
        MiraPresetButton(coordinator, slot, name) for slot, name in coordinator.presets.items()
    )
    async_add_entities(entities)


class MiraStopButton(MiraControlEntity, ButtonEntity):
    """Close every outlet, including a running preset. Always available."""

    _attr_translation_key = "stop"

    def __init__(self, coordinator: MiraCoordinator) -> None:
        """Bind the button."""
        super().__init__(coordinator, "stop")

    async def async_press(self) -> None:
        """Stop the valve."""
        await self.coordinator.async_stop()


class MiraPresetButton(MiraControlEntity, ButtonEntity):
    """Start one of the presets stored in the valve.

    A preset whose name was read at pairing is checked before it runs. On a valve that does
    not answer preset reads the button is named after its slot, runs the slot unchecked, and
    is disabled until the user enables it.
    """

    def __init__(self, coordinator: MiraCoordinator, slot: int, name: str | None) -> None:
        """Bind the button to its preset slot and name it after the preset when known."""
        super().__init__(coordinator, f"preset_{slot}")
        self._slot = slot
        if name is None:
            self._attr_translation_key = "preset_slot"
            self._attr_translation_placeholders = {"slot": str(slot)}
            self._attr_entity_registry_enabled_default = False
        else:
            self._attr_translation_key = "preset"
            self._attr_translation_placeholders = {"name": name}

    async def async_press(self) -> None:
        """Start the preset."""
        await self.coordinator.async_run_preset(self._slot)
