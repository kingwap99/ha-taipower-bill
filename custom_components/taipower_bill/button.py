"""Buttons: propose, confirm and cancel a 結算.

Three buttons rather than one, because Home Assistant has no native confirmation
step for services (only Lovelace's ``tap_action: confirmation:``, which does
nothing for an automation).  The safety therefore lives in the state machine:

* 「結算本期」 only *proposes* a 結算 -- it writes nothing.  The proposal shows up
  on the sensors, in a persistent notification, and as the two buttons below,
  and it expires by itself after 15 minutes.
* 「確認結算」 writes the proposal into the ledger.
* 「取消結算」 throws the proposal away.

Both of the last two are ``unavailable`` unless a proposal is open, so the
window in which a mistake is possible is impossible to miss.  Nothing is final
even after that: ``undo_close_period`` removes a wrong read date and
``update_reading`` rewrites one.
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
    async_add_entities(
        [
            ClosePeriodButton(coordinator, entry),
            ConfirmCloseButton(coordinator, entry),
            CancelCloseButton(coordinator, entry),
        ]
    )


class _TaipowerButton(CoordinatorEntity, ButtonEntity):
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


class ClosePeriodButton(_TaipowerButton):
    """Propose a 結算 for the current period (writes nothing on its own)."""

    _attr_translation_key = "close_period"
    _attr_icon = "mdi:cash-register"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{DOMAIN}.{entry.entry_id}.close_period"

    @property
    def extra_state_attributes(self) -> dict:
        pending = self._coordinator.pending_payload()
        return {
            "pending": bool(pending),
            "pending_read_date": (pending or {}).get("read_date"),
            "suggested_read_date": self._coordinator.suggested_read_date.isoformat(),
        }

    async def async_press(self) -> None:
        try:
            proposal = await self._coordinator.async_close_period()
        except Exception as err:
            _LOGGER.warning("結算本期被拒絕 (%s): %s", self._coordinator.label, err)
            raise
        bill = proposal.get("bill") or {}
        _LOGGER.info(
            "%s: 已提出待確認結算 %s → %s，%s 度 / 推定 %s 元；"
            "請按「確認結算」或呼叫 taipower_bill.confirm_close 才會寫入",
            self._coordinator.label,
            proposal.get("period_start"),
            proposal.get("read_date"),
            proposal.get("kwh"),
            bill.get("estimated_total"),
        )


class ConfirmCloseButton(_TaipowerButton):
    """Write the pending 結算 into the ledger."""

    _attr_translation_key = "confirm_close"
    _attr_icon = "mdi:check-circle-outline"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{DOMAIN}.{entry.entry_id}.confirm_close"

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._coordinator.pending_payload() is not None

    async def async_press(self) -> None:
        result = await self._coordinator.async_confirm_close()
        reading = result.get("reading") or {}
        _LOGGER.info(
            "%s: 已確認結算，抄見日 %s 寫入清單；新的一期從 %s 起算",
            self._coordinator.label,
            reading.get("date"),
            result.get("period_start"),
        )


class CancelCloseButton(_TaipowerButton):
    """Drop the pending 結算 without writing anything."""

    _attr_translation_key = "cancel_close"
    _attr_icon = "mdi:close-circle-outline"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{DOMAIN}.{entry.entry_id}.cancel_close"

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._coordinator.pending_payload() is not None

    async def async_press(self) -> None:
        result = await self._coordinator.async_cancel_close()
        _LOGGER.info(
            "%s: 已取消待確認的結算（沒有寫入任何資料）: %s",
            self._coordinator.label,
            result.get("cancelled"),
        )
