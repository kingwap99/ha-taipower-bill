"""Sensors for Taipower Bill."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import homeassistant.util.dt as dt_util
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTR_HISTORY, DOMAIN, SENSOR_DESCRIPTIONS
from .coordinator import TaipowerDataUpdateCoordinator

# Which key in coordinator.data feeds each sensor key.
_VALUE_KEYS: dict[str, str] = {
    "period_kwh": "kwh",
    "period_cost": "cost",
    "avg_daily_kwh": "avg_daily_kwh",
    "projected_period_kwh": "projected_kwh",
    "projected_period_cost": "projected_cost",
    "avg_price_per_kwh": "avg_price_per_kwh",
    "marginal_price_per_kwh": "marginal_price_per_kwh",
    "days_remaining": "days_remaining",
    "next_read_date": "next_read_date",
    "bill_variance": "bill_variance",
    "closed_periods": "history_len",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TaipowerDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        TaipowerBillSensor(coordinator, description, entry) for description in SENSOR_DESCRIPTIONS
    )


class TaipowerBillSensor(CoordinatorEntity, SensorEntity):
    """One reading of the current 台電 billing period."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, description, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}.{entry.entry_id}.{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"台電 {coordinator.label}",
            manufacturer="台灣電力公司",
            model="表燈非時間電價",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data or {}
        if self.entity_description.key == "closed_periods":
            return len(data.get(ATTR_HISTORY) or [])
        value = data.get(_VALUE_KEYS.get(self.entity_description.key, ""))
        if isinstance(value, float):
            return round(value, 3)
        return value

    @property
    def last_reset(self) -> datetime | None:
        """Start of the billing period -- the reset point of the ``total`` sensors.

        ``state_class: total`` means "accumulates, and restarts wherever
        last_reset moves".  本期度數 and 本期電費 do exactly that at 結算本期, so
        declaring the reset point keeps the recorder's long-term statistics
        honest instead of reading the new period as a huge negative change.  The
        other sensors are plain measurements and must not set this.
        """
        if self.entity_description.state_class != SensorStateClass.TOTAL:
            return None
        data = self.coordinator.data or {}
        start = data.get("period_start") or self.coordinator.period_start
        return dt_util.start_of_local_day(start)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        bill = data.get("bill")
        attrs: dict[str, Any] = {
            "label": data.get("label"),
            "usage_source": data.get("usage_source"),
            "period_start": data.get("period_start").isoformat() if data.get("period_start") else None,
            "period_end": data.get("period_end").isoformat() if data.get("period_end") else None,
            "last_read_date": self.coordinator.last_read_date.isoformat(),
            "days_elapsed": data.get("days_elapsed"),
            "cycle_days": self.coordinator.cycle_days,
            "config_warning": data.get("config_warning"),
            "config_issues": data.get("config_issues") or [],
        }
        if data.get("stats_coverage_start"):
            attrs["stats_coverage_start"] = data["stats_coverage_start"].isoformat()
        if data.get("stats_last_day"):
            # Today's statistics row is still partial, so this is "covered up to".
            attrs["stats_last_day"] = data["stats_last_day"].isoformat()
        if data.get("stats_error"):
            attrs["stats_error"] = data["stats_error"]
        if bill is not None:
            # bill.as_attributes() uses bill_window_* keys precisely so it cannot
            # overwrite the period_* values set above.
            attrs.update(bill.as_attributes())
        if self.entity_description.key == "closed_periods":
            attrs[ATTR_HISTORY] = data.get(ATTR_HISTORY) or []
        return attrs
