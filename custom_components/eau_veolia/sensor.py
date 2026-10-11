from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import VeoliaConfigEntry, VeoliaCoordinator, VeoliaData


def _french_date(day: date) -> str:
    return day.strftime("%d/%m/%Y")


@dataclass(frozen=True, kw_only=True)
class VeoliaSensorDescription(SensorEntityDescription):
    value: Callable[[VeoliaData], float | str | None]
    cost: bool = False


SENSORS = (
    VeoliaSensorDescription(
        key="index",
        translation_key="index",
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=3,
        value=lambda d: d.last.index_end_m3 if d.last else None,
    ),
    VeoliaSensorDescription(
        key="last_day",
        translation_key="last_day",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        value=lambda d: d.last.liters if d.last else None,
    ),
    VeoliaSensorDescription(
        key="month",
        translation_key="month",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        value=lambda d: d.month_liters,
    ),
    VeoliaSensorDescription(
        key="year",
        translation_key="year",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=3,
        value=lambda d: d.year_m3,
    ),
    VeoliaSensorDescription(
        key="average_30d",
        translation_key="average_30d",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        value=lambda d: d.average_30d_liters,
    ),
    VeoliaSensorDescription(
        key="last_reading",
        translation_key="last_reading",
        icon="mdi:calendar",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: _french_date(d.last.day) if d.last else None,
    ),
    VeoliaSensorDescription(
        key="cost_last_day",
        translation_key="cost_last_day",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        cost=True,
        value=lambda d: d.cost_last_day,
    ),
    VeoliaSensorDescription(
        key="cost_month",
        translation_key="cost_month",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        cost=True,
        value=lambda d: d.cost_month,
    ),
    VeoliaSensorDescription(
        key="cost_year",
        translation_key="cost_year",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        cost=True,
        value=lambda d: d.cost_year,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VeoliaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        VeoliaSensor(coordinator, description)
        for description in SENSORS
        if coordinator.tariff.enabled or not description.cost
    )


class VeoliaSensor(CoordinatorEntity[VeoliaCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: VeoliaSensorDescription

    def __init__(
        self, coordinator: VeoliaCoordinator, description: VeoliaSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        account = coordinator.data.account
        self._attr_unique_id = f"{account.id_abonnement}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, account.id_abonnement)},
            name="Compteur d'eau",
            manufacturer="Veolia",
            model="Télérelève",
            serial_number=account.numero_compteur or None,
        )

    @property
    def native_value(self) -> float | str | None:
        return self.entity_description.value(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        key = self.entity_description.key
        if key in ("index", "last_day") and data.last:
            return {
                "date_releve": _french_date(data.last.day),
                "fiabilite": "mesuré" if data.last.reliability == "M" else data.last.reliability,
            }
        if key == "cost_last_day" and data.last and data.tariff:
            return {
                "date_releve": _french_date(data.last.day),
                **(data.cost_last_day_detail or {}),
                **data.tariff.attributes(),
            }
        return None
