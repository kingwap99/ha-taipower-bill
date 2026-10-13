"""Constants for the Taipower Bill integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfTime

# The config keys and source modes are defined in validators.py (HA-free) so the
# standalone test suite can validate them; re-exported here for convenience.
from .validators import (
    CONF_CYCLE_DAYS,
    CONF_LABEL,
    CONF_LAST_READ_DATE,
    CONF_SOURCE_ENTITIES,
    CONF_SOURCE_MODE,
    DEFAULT_CYCLE_DAYS,
    SOURCE_MODE_MANUAL,
    SOURCE_MODE_STATISTICS,
)

__all__ = [
    "CONF_CYCLE_DAYS",
    "CONF_LABEL",
    "CONF_LAST_READ_DATE",
    "CONF_SOURCE_ENTITIES",
    "CONF_SOURCE_MODE",
    "DEFAULT_CYCLE_DAYS",
    "SOURCE_MODE_MANUAL",
    "SOURCE_MODE_STATISTICS",
]

DOMAIN = "taipower_bill"
PLATFORMS = ["sensor", "number", "button"]

CONF_PERIOD_MONTHS = "period_months"
CONF_SUMMER_START = "summer_start"
CONF_SUMMER_END = "summer_end"
CONF_SURCHARGE_PER_KWH = "surcharge_per_kwh"
CONF_FIXED_FEES_PER_PERIOD = "fixed_fees_per_period"
CONF_BUSINESS_TAX_RATE = "business_tax_rate"
CONF_MIN_CHARGE_PER_MONTH = "min_charge_per_month"
CONF_TARIFF_REVISION = "tariff_revision"

DEFAULT_PERIOD_MONTHS = 2
DEFAULT_SUMMER_START = "06-01"
DEFAULT_SUMMER_END = "09-30"
DEFAULT_MIN_CHARGE_PER_MONTH = 100.0

UNIT_TWD = "TWD"
UNIT_TWD_PER_KWH = "TWD/kWh"

ATTR_HISTORY = "history"
ATTR_SEGMENTS = "segments"
ATTR_TIERS = "tiers"

# v1 stored ``{"periods": [...]}`` (event driven: one row per 結算本期 press).
# v2 stores ``{"readings": [...]}`` (data driven: the list of actual 抄見日 that
# every billing period is derived from).  ``coordinator._async_migrate_store``
# keeps the v1 payload and ``TaipowerLedger.migrate`` reshapes it on load.
STORAGE_VERSION = 2
STORAGE_PREFIX = f"{DOMAIN}_ledger"


@dataclass(frozen=True)
class TaipowerSensorDescription(SensorEntityDescription):
    """Describe one Taipower bill sensor."""


# State classes are not free-form: HA 2026.10 validates every
# (device_class, state_class) pair against DEVICE_CLASS_STATE_CLASSES in
# homeassistant/components/sensor/const.py, and an illegal pair is a warning
# today and a hard error soon.  The rows that matter here are:
#
#   ENERGY   -> {total, total_increasing}
#   MONETARY -> {total}
#   DURATION -> any state class
#
# A *derived* figure (an average, a projection, a price per kWh) therefore must
# not claim device_class=energy/monetary together with state_class=measurement.
# Only the two figures that genuinely accumulate across the billing period --
# 本期度數 and 本期電費 -- keep a state class, declared as ``total`` so the
# recorder can keep long-term statistics that restart at 本期起算日 (the sensor
# supplies the matching ``last_reset``).
SENSOR_DESCRIPTIONS: tuple[TaipowerSensorDescription, ...] = (
    TaipowerSensorDescription(
        key="period_kwh",
        translation_key="period_kwh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
    ),
    TaipowerSensorDescription(
        key="period_cost",
        translation_key="period_cost",
        native_unit_of_measurement=UNIT_TWD,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
    ),
    TaipowerSensorDescription(
        key="avg_daily_kwh",
        translation_key="avg_daily_kwh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=2,
    ),
    TaipowerSensorDescription(
        key="projected_period_kwh",
        translation_key="projected_period_kwh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=0,
    ),
    TaipowerSensorDescription(
        key="projected_period_cost",
        translation_key="projected_period_cost",
        native_unit_of_measurement=UNIT_TWD,
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=0,
    ),
    TaipowerSensorDescription(
        key="avg_price_per_kwh",
        translation_key="avg_price_per_kwh",
        native_unit_of_measurement=UNIT_TWD_PER_KWH,
        suggested_display_precision=3,
    ),
    TaipowerSensorDescription(
        key="marginal_price_per_kwh",
        translation_key="marginal_price_per_kwh",
        native_unit_of_measurement=UNIT_TWD_PER_KWH,
        suggested_display_precision=2,
    ),
    TaipowerSensorDescription(
        key="days_remaining",
        translation_key="days_remaining",
        native_unit_of_measurement=UnitOfTime.DAYS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    TaipowerSensorDescription(
        key="next_read_date",
        translation_key="next_read_date",
        device_class=SensorDeviceClass.DATE,
    ),
    TaipowerSensorDescription(
        key="bill_variance",
        translation_key="bill_variance",
        native_unit_of_measurement=UNIT_TWD,
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=0,
    ),
    TaipowerSensorDescription(
        key="closed_periods",
        translation_key="closed_periods",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:file-document-multiple-outline",
    ),
    # 「上一期／上上期」: fixed amounts from already-closed periods, so they carry
    # a device_class but no state_class.  They do not accumulate and must not
    # claim ``measurement`` under energy/monetary (see the note above).
    TaipowerSensorDescription(
        key="last_period_kwh",
        translation_key="last_period_kwh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        icon="mdi:history",
        suggested_display_precision=1,
    ),
    TaipowerSensorDescription(
        key="last_period_cost",
        translation_key="last_period_cost",
        native_unit_of_measurement=UNIT_TWD,
        device_class=SensorDeviceClass.MONETARY,
        icon="mdi:receipt-text-check-outline",
        suggested_display_precision=0,
    ),
    TaipowerSensorDescription(
        key="last_period_avg_price",
        translation_key="last_period_avg_price",
        native_unit_of_measurement=UNIT_TWD_PER_KWH,
        icon="mdi:cash-multiple",
        suggested_display_precision=3,
    ),
    TaipowerSensorDescription(
        key="prev_period_kwh",
        translation_key="prev_period_kwh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        icon="mdi:history",
        suggested_display_precision=1,
    ),
    TaipowerSensorDescription(
        key="prev_period_cost",
        translation_key="prev_period_cost",
        native_unit_of_measurement=UNIT_TWD,
        device_class=SensorDeviceClass.MONETARY,
        icon="mdi:receipt-text-check-outline",
        suggested_display_precision=0,
    ),
    TaipowerSensorDescription(
        key="pending_close_date",
        translation_key="pending_close_date",
        device_class=SensorDeviceClass.DATE,
        icon="mdi:calendar-question",
    ),
)
