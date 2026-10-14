"""Buttons: propose, confirm, cancel and undo a 結算.

Four buttons rather than one, because Home Assistant has no native confirmation
step for services (only Lovelace's ``tap_action: confirmation:``, which does
nothing for an automation).  The safety therefore lives in the state machine:

* 「結算本期」 only *proposes* a 結算 -- it writes nothing.  The proposal shows up
  on the sensors, in a persistent notification, and as the two buttons below,
  and it expires by itself after 15 minutes.
* 「確認結算」 writes the proposal into the ledger.
* 「取消結算」 throws the proposal away.
* 「撤銷最後一期」 removes the newest *written* 抄見日 and re-opens the period it
  closed.  That is the recovery path for a mis-tap that got past confirm: a
  button entity cannot ask "are you sure?" (HA has no confirmation for a
  button press -- only a card can, via ``tap_action: confirmation:``), so the
  safety here is that the button is ``unavailable`` unless there really is a
  written period to take back, and it names the date it will remove in its
  attributes.

The first three are ``unavailable`` unless a proposal is open, so the window in
which a mistake is possible is impossible to miss.  Nothing is final even after
that: ``undo_close_period`` removes a wrong read date and ``update_reading``
rewrites one.
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
            UndoCloseButton(coordinator, entry),
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


class UndoCloseButton(_TaipowerButton):
    """Remove the newest written 抄見日 and re-open the period it closed.

    The 「不小心按到結算」 rescue, as a plain button so it can live on a dashboard
    or be called from an automation.  It is deliberately *not* available when
    there is nothing written yet (``last_read_date is None``: every read date is
    still in the future) or while a proposal is open -- pressing it there would
    look like it did something when the only meaningful action is 「取消結算」.
    """

    _attr_translation_key = "undo_close_period"
    _attr_icon = "mdi:undo-variant"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{DOMAIN}.{entry.entry_id}.undo_close_period"

    @property
    def _undo_date(self):
        """The read date this press would remove, or ``None``."""
        if self._coordinator.pending is not None:
            return None
        return self._coordinator.last_read_date

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._undo_date is not None

    @property
    def extra_state_attributes(self) -> dict:
        date = self._undo_date
        return {
            # Named, because a button entity cannot ask for confirmation: the
            # card shows this date in its "確定要撤銷 X 嗎？" dialog, and an
            # automation can log it.
            "will_remove": date.isoformat() if date else None,
            "readings": [r.get("date") for r in self._coordinator.readings],
            "can_undo": date is not None,
        }

    async def async_press(self) -> None:
        result = await self._coordinator.async_undo_close_period()
        removed = result.get("removed") or {}
        _LOGGER.info(
            "%s: 已撤銷抄見日 %s；本期回到 %s → %s",
            self._coordinator.label,
            removed.get("date"),
            result.get("period_start"),
            result.get("period_end"),
        )
