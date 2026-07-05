"""Grounding-caution binary sensor for Engine Moisture Monitor."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EngineMoistureCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EngineMoistureCoordinator = entry.runtime_data
    async_add_entities([
        GroundingCautionBinarySensor(coordinator, entry),
        SensorStaleBinarySensor(coordinator, entry),
    ])


class GroundingCautionBinarySensor(
    CoordinatorEntity[EngineMoistureCoordinator], BinarySensorEntity
):
    """Fires only when real wetting was followed by a month grounded (data-aware
    'fly monthly')."""

    _attr_has_entity_name = True
    _attr_translation_key = "grounding_caution"
    _attr_name = "Cam grounding caution"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:alert-decagram"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_grounding_caution"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Engine Moisture Monitor",
            manufacturer="Lycoming engine-moisture skill",
            model="Cam-corrosion exposure",
        )

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        return None if data is None else bool(data.get("grounding_caution"))

    @property
    def extra_state_attributes(self) -> dict | None:
        data = self.coordinator.data or {}
        return {
            "reason": data.get("gc_reason"),
            "wet_hours_since_flight": data.get("gc_wet_hours"),
            "days_grounded": data.get("gc_days_grounded"),
        }


class SensorStaleBinarySensor(
    CoordinatorEntity[EngineMoistureCoordinator], BinarySensorEntity
):
    """On while the cowl feed is stale/offline and the model is running on the
    station-transfer gap-fill estimate instead of real sensor data."""

    _attr_has_entity_name = True
    _attr_translation_key = "sensor_stale"
    _attr_name = "Cowl sensor offline"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:access-point-network-off"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_sensor_stale"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Engine Moisture Monitor",
            manufacturer="Lycoming engine-moisture skill",
            model="Cam-corrosion exposure",
        )

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        return None if data is None else bool(data.get("sensor_stale"))

    @property
    def extra_state_attributes(self) -> dict | None:
        data = self.coordinator.data or {}
        return {
            "gap_filled_hours": data.get("gap_filled_hours"),
            "gapfill_error": data.get("gapfill_error"),
            "last_real_reading": data.get("last_real_reading"),
        }
