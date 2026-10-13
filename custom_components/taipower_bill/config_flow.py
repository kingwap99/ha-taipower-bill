"""Config flow for Taipower Bill."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import voluptuous as vol

import homeassistant.util.dt as dt_util
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    DateSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_BUSINESS_TAX_RATE,
    CONF_CYCLE_DAYS,
    CONF_FIXED_FEES_PER_PERIOD,
    CONF_LABEL,
    CONF_LAST_READ_DATE,
    CONF_MIN_CHARGE_PER_MONTH,
    CONF_PERIOD_MONTHS,
    CONF_SOURCE_ENTITIES,
    CONF_SOURCE_MODE,
    CONF_SUMMER_END,
    CONF_SUMMER_START,
    CONF_SURCHARGE_PER_KWH,
    CONF_TARIFF_REVISION,
    DEFAULT_CYCLE_DAYS,
    DEFAULT_MIN_CHARGE_PER_MONTH,
    DEFAULT_PERIOD_MONTHS,
    DEFAULT_SUMMER_END,
    DEFAULT_SUMMER_START,
    DOMAIN,
    SOURCE_MODE_MANUAL,
    SOURCE_MODE_STATISTICS,
)
from .tariffs import REVISIONS
from .validators import validate_basics

OPT_RESET_HISTORY = "reset_history"


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return cleaned or "meter"


class TaipowerBillConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create one config entry per 台電 meter (一戶一個)."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return TaipowerBillOptionsFlow(config_entry)

    def _basics_schema(self, values: dict[str, Any] | None = None, allow_reset: bool = False):
        values = values or {}
        last_read = values.get(CONF_LAST_READ_DATE)
        if not last_read:
            last_read = (dt_util.now() - timedelta(days=30)).date().isoformat()
        fields: dict[Any, Any] = {
            vol.Required(CONF_LABEL, default=values.get(CONF_LABEL, "")): TextSelector(),
            vol.Required(
                CONF_SOURCE_ENTITIES, default=values.get(CONF_SOURCE_ENTITIES, [])
            ): EntitySelector(
                EntitySelectorConfig(
                    domain="sensor",
                    device_class=["energy"],
                    multiple=True,
                )
            ),
            vol.Required(
                CONF_SOURCE_MODE, default=values.get(CONF_SOURCE_MODE, SOURCE_MODE_STATISTICS)
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[SOURCE_MODE_STATISTICS, SOURCE_MODE_MANUAL],
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="source_mode",
                )
            ),
            vol.Required(CONF_LAST_READ_DATE, default=last_read): DateSelector(),
            vol.Optional(
                CONF_CYCLE_DAYS, default=values.get(CONF_CYCLE_DAYS, DEFAULT_CYCLE_DAYS)
            ): NumberSelector(
                NumberSelectorConfig(min=28, max=92, step=1, mode=NumberSelectorMode.BOX)
            ),
        }
        if allow_reset:
            fields[vol.Optional(OPT_RESET_HISTORY, default=False)] = BooleanSelector()
        return vol.Schema(fields)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = validate_basics(user_input, dt_util.now().date())
            if not errors:
                label = (user_input[CONF_LABEL] or "").strip()
                await self.async_set_unique_id(f"{DOMAIN}.{_slug(label)}")
                self._abort_if_unique_id_configured()
                data = {**user_input, CONF_PERIOD_MONTHS: 2}
                data[CONF_LABEL] = label
                return self.async_create_entry(title=label, data=data)

        return self.async_show_form(
            step_id="user", data_schema=self._basics_schema(user_input), errors=errors
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        current = {**entry.data, **entry.options} if entry else {}
        errors: dict[str, str] = {}
        if user_input is not None:
            reset_history = bool(user_input.pop(OPT_RESET_HISTORY, False))
            errors = validate_basics(user_input, dt_util.now().date())
            if not errors:
                if reset_history and entry is not None:
                    await self._async_clear_history(entry)
                user_input[CONF_LABEL] = (user_input[CONF_LABEL] or "").strip()
                return self.async_update_reload_and_abort(entry, data={**current, **user_input})

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._basics_schema(current, allow_reset=True),
            errors=errors,
        )

    async def _async_clear_history(self, entry: ConfigEntry) -> None:
        """Wipe the closed-period ledger (bad rows from a mis-set 抄見日)."""
        coordinator = (self.hass.data.get(DOMAIN) or {}).get(entry.entry_id, {}).get("coordinator")
        if coordinator is not None:
            await coordinator.async_reset_ledger()


class TaipowerBillOptionsFlow(OptionsFlow):
    """Money knobs: 夏月窗口、附加費、營業稅、費率版本."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        cur = {**self._entry.data, **self._entry.options}
        revision_options = ["latest"] + [r.key for r in REVISIONS]
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SUMMER_START, default=cur.get(CONF_SUMMER_START, DEFAULT_SUMMER_START)
                    ): TextSelector(),
                    vol.Optional(
                        CONF_SUMMER_END, default=cur.get(CONF_SUMMER_END, DEFAULT_SUMMER_END)
                    ): TextSelector(),
                    vol.Optional(
                        CONF_TARIFF_REVISION, default=cur.get(CONF_TARIFF_REVISION, "latest")
                    ): SelectSelector(
                        SelectSelectorConfig(options=revision_options, mode=SelectSelectorMode.DROPDOWN)
                    ),
                    vol.Optional(
                        CONF_SURCHARGE_PER_KWH, default=cur.get(CONF_SURCHARGE_PER_KWH, 0.0)
                    ): NumberSelector(
                        NumberSelectorConfig(min=0, max=5, step=0.001, mode=NumberSelectorMode.BOX)
                    ),
                    vol.Optional(
                        CONF_FIXED_FEES_PER_PERIOD, default=cur.get(CONF_FIXED_FEES_PER_PERIOD, 0.0)
                    ): NumberSelector(
                        NumberSelectorConfig(min=0, max=5000, step=1, mode=NumberSelectorMode.BOX)
                    ),
                    vol.Optional(
                        CONF_BUSINESS_TAX_RATE, default=cur.get(CONF_BUSINESS_TAX_RATE, 0.0)
                    ): NumberSelector(
                        NumberSelectorConfig(min=0, max=0.2, step=0.001, mode=NumberSelectorMode.BOX)
                    ),
                    vol.Optional(
                        CONF_MIN_CHARGE_PER_MONTH,
                        default=cur.get(CONF_MIN_CHARGE_PER_MONTH, DEFAULT_MIN_CHARGE_PER_MONTH),
                    ): NumberSelector(
                        NumberSelectorConfig(min=0, max=1000, step=1, mode=NumberSelectorMode.BOX)
                    ),
                    vol.Optional(
                        CONF_PERIOD_MONTHS, default=cur.get(CONF_PERIOD_MONTHS, DEFAULT_PERIOD_MONTHS)
                    ): NumberSelector(
                        NumberSelectorConfig(min=1, max=3, step=1, mode=NumberSelectorMode.BOX)
                    ),
                }
            ),
        )
