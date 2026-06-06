"""Sensor entities for Engine Moisture Monitor (read from the coordinator)."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EngineMoistureCoordinator


@dataclass(frozen=True, kw_only=True)
class EMSensorDescription(SensorEntityDescription):
    """Describes a sensor backed by a coordinator.data key (or callable)."""

    value_fn: Callable[[EngineMoistureCoordinator], Any] | None = None
    attrs_fn: Callable[[dict], dict] | None = None


SENSORS: tuple[EMSensorDescription, ...] = (
    EMSensorDescription(
        key="film_hours_since_flight",
        translation_key="film_hours_since_flight",
        name="Cam wet-hours since last flight",
        icon="mdi:water-percent",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        attrs_fn=lambda d: {
            "days_since_flight": d.get("days_since_flight"),
            "wet_hours_realistic": d.get("wet_hours_realistic"),
            "ambient_damp_hours_ub": d.get("ambient_damp_hours_ub"),
            "sub_dew_hours": d.get("sub_dew_h"),
            "close_call_hours": d.get("close_call_h"),
            "last_flight": d.get("last_flight"),
            "latest_temp_c": d.get("latest_temp_c"),
            "latest_rh_pct": d.get("latest_rh_pct"),
            "latest_reading": d.get("latest_reading"),
        },
    ),
    EMSensorDescription(
        key="days_since_flight",
        translation_key="days_since_flight",
        name="Days since last flight",
        icon="mdi:calendar-clock",
        native_unit_of_measurement=UnitOfTime.DAYS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EMSensorDescription(
        key="last_flight",
        translation_key="last_flight",
        name="Last engine run",
        icon="mdi:airplane-takeoff",
        attrs_fn=lambda d: {"flight_count": d.get("flight_count")},
    ),
    EMSensorDescription(
        key="last_run",
        translation_key="last_run",
        name="Last model run",
        icon="mdi:run",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.last_run,
        attrs_fn=lambda d: {"last_alert": d.get("last_alert"),
                            "alert_reason": d.get("alert_reason")},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EngineMoistureCoordinator = entry.runtime_data
    async_add_entities(EngineMoistureSensor(coordinator, entry, desc) for desc in SENSORS)


class EngineMoistureSensor(CoordinatorEntity[EngineMoistureCoordinator], SensorEntity):
    """A sensor reading one value out of the coordinator."""

    _attr_has_entity_name = True
    entity_description: EMSensorDescription

    def __init__(self, coordinator, entry, description: EMSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Engine Moisture Monitor",
            manufacturer="Lycoming engine-moisture skill",
            model="Cam-corrosion exposure",
        )

    @property
    def native_value(self):
        if self.entity_description.value_fn is not None:
            return self.entity_description.value_fn(self.coordinator)
        data = self.coordinator.data or {}
        return data.get(self.entity_description.key)

    @property
    def extra_state_attributes(self) -> dict | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data or {})
