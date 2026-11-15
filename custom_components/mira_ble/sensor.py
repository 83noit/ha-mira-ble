"""Water temperature, flow, the last run, a water total and the outlets' own limits."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from mira_ble import LastUsage

from .coordinator import MiraConfigEntry, MiraCoordinator, OutletInfo, ValveData
from .entity import MiraEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class MiraSensorDescription(SensorEntityDescription):
    """A sensor read from one poll's data; None means the valve has not reported it."""

    value_fn: Callable[[ValveData], float | int | None]


@dataclass(frozen=True, kw_only=True)
class MiraOutletSensorDescription(SensorEntityDescription):
    """A sensor showing one of an outlet's own limits."""

    value_fn: Callable[[OutletInfo], float | int | None]


SENSORS: tuple[MiraSensorDescription, ...] = (
    MiraSensorDescription(
        key="water_temperature",
        translation_key="water_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda data: data.state.water_temperature,
    ),
    MiraSensorDescription(
        key="flow",
        translation_key="flow",
        device_class=SensorDeviceClass.VOLUME_FLOW_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfVolumeFlowRate.LITERS_PER_MINUTE,
        suggested_display_precision=1,
        value_fn=lambda data: data.state.flow,
    ),
    MiraSensorDescription(
        key="last_run_duration",
        translation_key="last_run_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=1,
        value_fn=lambda data: data.last_usage.duration if data.last_usage else None,
    ),
    MiraSensorDescription(
        key="last_run_volume",
        translation_key="last_run_volume",
        device_class=SensorDeviceClass.VOLUME,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        value_fn=lambda data: data.last_usage.volume if data.last_usage else None,
    ),
    MiraSensorDescription(
        key="last_run_temperature",
        translation_key="last_run_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda data: data.last_usage.temperature if data.last_usage else None,
    ),
    MiraSensorDescription(
        key="error_code",
        translation_key="error_code",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.state.error_code,
    ),
)

OUTLET_SENSORS: tuple[MiraOutletSensorDescription, ...] = (
    MiraOutletSensorDescription(
        key="max_temperature",
        translation_key="outlet_max_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda outlet: outlet.max_temperature,
    ),
    MiraOutletSensorDescription(
        key="max_duration",
        translation_key="outlet_max_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        entity_category=EntityCategory.DIAGNOSTIC,
        # Zero means the limit was not stored; it is the valve's own backstop when set.
        value_fn=lambda outlet: outlet.max_duration or None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: MiraConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [MiraSensor(coordinator, description) for description in SENSORS]
    entities.append(MiraWaterTotalSensor(coordinator))
    entities.extend(
        MiraOutletSensor(coordinator, number, description)
        for number in coordinator.outlets
        for description in OUTLET_SENSORS
    )
    async_add_entities(entities)


class MiraSensor(MiraEntity, SensorEntity):
    """A sensor of one valve."""

    entity_description: MiraSensorDescription

    def __init__(self, coordinator: MiraCoordinator, description: MiraSensorDescription) -> None:
        """Bind the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | int | None:
        """Return the reading; unknown when the valve has not reported it."""
        return self.entity_description.value_fn(self.coordinator.data)


class MiraOutletSensor(MiraEntity, SensorEntity):
    """One of an outlet's limits, as the valve last reported it."""

    entity_description: MiraOutletSensorDescription

    def __init__(
        self, coordinator: MiraCoordinator, number: int, description: MiraOutletSensorDescription
    ) -> None:
        """Bind the sensor to its outlet."""
        super().__init__(coordinator, f"outlet_{number}_{description.key}")
        self.entity_description = description
        self._number = number
        self._attr_translation_placeholders = {"number": str(number)}

    @property
    def native_value(self) -> float | int | None:
        """Return the limit; unknown when the outlet is gone or the limit unset."""
        outlet = self.coordinator.outlets.get(self._number)
        return None if outlet is None else self.entity_description.value_fn(outlet)


class MiraWaterTotalSensor(MiraEntity, RestoreSensor):
    """Litres used, summed from the valve's record of each finished run.

    The valve keeps only its last run. Each time a record different from the one last seen
    appears, its volume is added. The first record seen after a start is a baseline and is
    not counted (it may already have been), two identical runs in a row count once, and runs
    that finish while the valve is out of reach collapse to the latest.
    """

    _attr_translation_key = "water_total"
    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS

    def __init__(self, coordinator: MiraCoordinator) -> None:
        """Bind the sensor."""
        super().__init__(coordinator, "water_total")
        self._total = 0.0
        self._seen: LastUsage | None = None

    async def async_added_to_hass(self) -> None:
        """Carry the total across restarts."""
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and isinstance(last.native_value, int | float):
            self._total = float(last.native_value)
        self._seen = self.coordinator.data.last_usage

    @callback
    def _handle_coordinator_update(self) -> None:
        record = self.coordinator.data.last_usage
        if record is not None and record != self._seen:
            if self._seen is not None:
                self._total += record.volume
            self._seen = record
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float:
        """Return the litres counted so far."""
        return self._total
