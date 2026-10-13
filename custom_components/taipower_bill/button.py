"""Button: close the current billing period into history.

Press this the day the 電費單 arrives. It freezes the period, records the
reconciliation against ``actual_bill`` if you entered one, and advances 抄見日
by ``cycle_days`` so the next period starts counting immediately.

Because every press permanently moves 上期抄見日 forward, the coordinator refuses
to close a period that has not reached its projected 抄見日 (see
``TaipowerDataUpdateCoordinator._assert_closeable``).  The refusal surfaces as an
error toast, which is the point: pressing this button repeatedly used to walk
抄見日 years into the future and blank every sensor.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TaipowerDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: TaipowerDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([ClosePeriodButton(coordinator, entry)])


class ClosePeriodButton(CoordinatorEntity, ButtonEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "close_period"

    def __init__(self, coordinator: TaipowerDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._attr_unique_id = f"{DOMAIN}.{entry.entry_id}.close_period"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"台電 {coordinator.label}",
            manufacturer="台灣電力公司",
            model="表燈非時間電價",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_press(self) -> None:
        try:
            record = await self._coordinator.async_close_period()
        except Exception as err:
            _LOGGER.warning("結算本期被拒絕 (%s): %s", self._coordinator.label, err)
            raise
        _LOGGER.info(
            "%s: 已結算 %s → %s，%s 度 / 推定 %s 元；新抄見日 %s",
            self._coordinator.label,
            record["period_start"],
            record["period_end"],
            record["kwh"],
            record["estimated_total"],
            self._coordinator.last_read_date,
        )
