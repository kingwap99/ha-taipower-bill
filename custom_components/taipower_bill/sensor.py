"""Sensors for Taipower Bill.

Every sensor of one config entry publishes the *same* attribute block (label,
period window, the read-date list, the per-period history, the pending 結算,
the tier/segment detail of the current period …).  That is deliberate: the
Lovelace card then works whichever entity it is pointed at, and the recorder
de-duplicates identical attribute payloads (``states_attributes`` is keyed by
the payload hash), so repeating the block across the entities does not multiply
the rows in the database.
"""

from __future__ import annotations

from datetime import date, datetime
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
    "last_period_kwh": "last_period_kwh",
    "last_period_cost": "last_period_cost",
    "last_period_avg_price": "last_period_avg_price",
    "prev_period_kwh": "prev_period_kwh",
    "prev_period_cost": "prev_period_cost",
    "pending_close_date": "pending_close_date",
}

#: Sensors whose state is a date.  A HA ``date`` sensor must not carry a unit
#: and must be given an ISO string (or a date), not a float.
_DATE_KEYS = {"next_read_date", "pending_close_date"}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


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
        key = self.entity_description.key
        data = self.coordinator.data or {}
        if key == "closed_periods":
            return len(data.get(ATTR_HISTORY) or [])
        value = data.get(_VALUE_KEYS.get(key, ""))
        if key in _DATE_KEYS:
            return _iso(value)
        if isinstance(value, float):
            return round(value, 3)
        return value

    @property
    def last_reset(self) -> datetime | None:
        """Start of the billing period -- the reset point of the ``total`` sensors.

        ``state_class: total`` means "accumulates, and restarts wherever
        last_reset moves".  本期度數 and 本期電費 do exactly that when a 抄見日 is
        written, so declaring the reset point keeps the recorder's long-term
        statistics honest instead of reading the new period as a huge negative
        change.  The other sensors are plain measurements and must not set this
        (``state_attributes`` raises if a non-``total`` sensor has it).
        """
        if self.entity_description.state_class != SensorStateClass.TOTAL:
            return None
        start = (self.coordinator.data or {}).get("period_start") or self.coordinator.period_start
        if start is None:
            # No usable window yet: ``total`` without a reset point is still
            # legal (it then behaves like total_increasing).
            return None
        return dt_util.start_of_local_day(start)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        bill = data.get("bill")
        attrs: dict[str, Any] = {
            "label": data.get("label"),
            "usage_source": data.get("usage_source"),
            "period_start": _iso(data.get("period_start")),
            "period_end": _iso(data.get("period_end")),
            "next_read_date": _iso(data.get("next_read_date")),
            "last_read_date": _iso(data.get("last_read_date")),
            "suggested_read_date": _iso(data.get("suggested_read_date")),
            "days_elapsed": data.get("days_elapsed"),
            "days_in_period": data.get("days_in_period"),
            "days_remaining": data.get("days_remaining"),
            "cycle_days": data.get("cycle_days") or self.coordinator.cycle_days,
            "period_has_started": data.get("period_has_started"),
            "period_has_ended": data.get("period_has_ended"),
            "period_note": data.get("period_note"),
            "cost": data.get("cost"),
            "avg_daily_kwh": data.get("avg_daily_kwh"),
            "projected_kwh": data.get("projected_kwh"),
            "projected_cost": data.get("projected_cost"),
            "bill_variance": data.get("bill_variance"),
            "config_warning": data.get("config_warning"),
            "config_issues": data.get("config_issues") or [],
            ATTR_HISTORY: data.get(ATTR_HISTORY) or [],
            "history_len": data.get("history_len") or 0,
            "last_period_start": data.get("last_period_start"),
            "last_period_end": data.get("last_period_end"),
            "last_period_days": data.get("last_period_days"),
            "last_period_kwh": data.get("last_period_kwh"),
            "last_period_cost": data.get("last_period_cost"),
            "last_period_estimated_cost": data.get("last_period_estimated_cost"),
            "last_period_actual_cost": data.get("last_period_actual_cost"),
            "last_period_variance": data.get("last_period_variance"),
            "last_period_avg_price": data.get("last_period_avg_price"),
            "last_period_source": data.get("last_period_source"),
            "prev_period_start": data.get("prev_period_start"),
            "prev_period_end": data.get("prev_period_end"),
            "prev_period_kwh": data.get("prev_period_kwh"),
            "prev_period_cost": data.get("prev_period_cost"),
            "prev_period_avg_price": data.get("prev_period_avg_price"),
            "readings": data.get("readings") or [],
            "pending_close": data.get("pending_close"),
            "pending_close_date": data.get("pending_close_date"),
            "pending_close_seconds_left": data.get("pending_close_seconds_left"),
            "actual_bill": data.get("actual_bill"),
            "stats_coverage_start": _iso(data.get("stats_coverage_start")),
            "stats_last_day": _iso(data.get("stats_last_day")),
            "stats_error": data.get("stats_error"),
            "stats_write_error": data.get("stats_write_error"),
        }
        if bill is not None:
            # as_attributes() brings kwh / energy_cost / tiers / segments and
            # names its own window bill_window_* precisely so it cannot clash
            # with the period_* values above.
            attrs.update(bill.as_attributes())
        return attrs
