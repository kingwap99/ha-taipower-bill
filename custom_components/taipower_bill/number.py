"""Number entities: key in what the paper bill says.

``manual_period_kwh``  -- 本期抄見度數 minus 上期抄見度數, when you are running
in manual source mode (e.g. while the Vue telemetry is down).
``actual_bill``        -- 本期電費金額 from the bill, used for reconciliation.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, UNIT_TWD
from .coordinator import TaipowerDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: TaipowerDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [
            ManualKwhNumber(coordinator, entry),
            ActualBillNumber(coordinator, entry),
        ]
    )


class _TaipowerNumber(CoordinatorEntity, NumberEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: TaipowerDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"台電 {coordinator.label}",
            manufacturer="台灣電力公司",
            model="表燈非時間電價",
            entry_type=DeviceEntryType.SERVICE,
        )


class ManualKwhNumber(_TaipowerNumber):
    _attr_translation_key = "manual_period_kwh"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 99999
    _attr_step = 1

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{DOMAIN}.{entry.entry_id}.manual_period_kwh"

    @property
    def native_value(self) -> float | None:
        return self._coordinator.manual_kwh

    async def async_set_native_value(self, value: float) -> None:
        self._coordinator.manual_kwh = float(value)
        await self._coordinator.async_request_refresh()


class ActualBillNumber(_TaipowerNumber):
    _attr_translation_key = "actual_bill"
    _attr_native_unit_of_measurement = UNIT_TWD
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 999999
    _attr_step = 1

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{DOMAIN}.{entry.entry_id}.actual_bill"

    @property
    def native_value(self) -> float | None:
        return self._coordinator.actual_bill

    async def async_set_native_value(self, value: float) -> None:
        self._coordinator.actual_bill = float(value)
        await self._coordinator.async_request_refresh()
