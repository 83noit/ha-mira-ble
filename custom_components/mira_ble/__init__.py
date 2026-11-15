"""Mira Bluetooth: Mira digital showers (Platinum, Activate, Mode) over Bluetooth LE."""

from __future__ import annotations

from bleak_retry_connector import close_stale_connections_by_address
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import service
from homeassistant.helpers.typing import ConfigType
import probatio

from .const import ATTR_FLOW, ATTR_TEMPERATURE, DOMAIN, SERVICE_START
from .coordinator import MiraConfigEntry, MiraCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the start action, which exists whether or not a valve is loaded."""
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_START,
        entity_domain=Platform.SWITCH,
        schema={
            probatio.Optional(ATTR_TEMPERATURE): probatio.Coerce(float),
            probatio.Optional(ATTR_FLOW): probatio.Coerce(float),
        },
        func="async_start",
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: MiraConfigEntry) -> bool:
    """Set up a valve from a config entry.

    The first poll must succeed: a valve that cannot be reached raises ConfigEntryNotReady
    and Home Assistant retries the setup by itself; one that refuses the stored pairing
    raises ConfigEntryAuthFailed and Home Assistant asks for pairing again.
    """
    # A link left open by an earlier run (a restart mid-poll on a local adapter) would hold the
    # valve's one connection and fail every poll.
    await close_stale_connections_by_address(entry.data[CONF_ADDRESS])
    coordinator = MiraCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    _async_remove_stale_entities(hass, entry, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


@callback
def _async_remove_stale_entities(
    hass: HomeAssistant, entry: MiraConfigEntry, coordinator: MiraCoordinator
) -> None:
    """Remove the entities of presets and outlets the valve no longer has.

    Presets can be deleted in the vendor app, and pairing again re-reads them; without this
    their buttons would stay behind, unavailable for good. The stored lists only ever shrink
    on a clean re-read (the config flow keeps them when a read failed), so nothing is removed
    on the strength of a read that went wrong.
    """
    base = coordinator.unique_base
    current = {f"{base}_preset_{slot}" for slot in coordinator.presets}
    for number in coordinator.outlets:
        current |= {
            f"{base}_outlet_{number}",
            f"{base}_outlet_{number}_max_temperature",
            f"{base}_outlet_{number}_max_duration",
        }
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        per_item = entity.unique_id.startswith((f"{base}_preset_", f"{base}_outlet_"))
        if per_item and entity.unique_id not in current:
            registry.async_remove(entity.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: MiraConfigEntry) -> bool:
    """Unload a config entry and close the link to the valve.

    Water that is running keeps running: unloading is not a stop. The valve's own maximum
    run time ends it.
    """
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded
