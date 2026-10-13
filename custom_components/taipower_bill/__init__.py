"""Set up the Taipower Bill integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, PLATFORMS
from .coordinator import TaipowerDataUpdateCoordinator
from .validators import parse_date

_LOGGER = logging.getLogger(__name__)

SERVICE_CLOSE_PERIOD = "close_period"
SERVICE_SET_LAST_READ = "set_last_read_date"
SERVICE_RESET_LEDGER = "reset_ledger"

CLOSE_PERIOD_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): str,
        vol.Optional("device_id"): vol.Any(str, [str]),
        vol.Optional("entity_id"): vol.Any(str, [str]),
        vol.Optional("force", default=False): vol.Coerce(bool),
        vol.Optional("actual_kwh"): vol.Coerce(float),
        vol.Optional("actual_bill"): vol.Coerce(float),
    }
)

SET_LAST_READ_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): str,
        vol.Optional("device_id"): vol.Any(str, [str]),
        vol.Optional("entity_id"): vol.Any(str, [str]),
        vol.Required("date"): vol.Any(str, object),
        vol.Optional("reset_history", default=False): vol.Coerce(bool),
    }
)

RESET_LEDGER_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): str,
        vol.Optional("device_id"): vol.Any(str, [str]),
        vol.Optional("entity_id"): vol.Any(str, [str]),
        vol.Optional("clear_baseline", default=True): vol.Coerce(bool),
    }
)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _resolve_coordinators(
    hass: HomeAssistant, call: ServiceCall
) -> dict[str, TaipowerDataUpdateCoordinator]:
    """Map a service target (device/entity/entry_id) to our coordinators.

    ``target:`` in a service call is merged into ``call.data`` as
    ``device_id``/``entity_id``, so handling those keys covers both the YAML
    ``target:`` form and explicit fields.
    """
    entries: set[str] = set()
    if entry_id := call.data.get("entry_id"):
        entries.add(str(entry_id))

    entity_ids = _as_list(call.data.get("entity_id"))
    device_ids = _as_list(call.data.get("device_id"))

    ent_reg = er.async_get(hass)
    for entity_id in entity_ids:
        reg_entry = ent_reg.async_get(entity_id)
        if reg_entry and reg_entry.device_id:
            device_ids.append(reg_entry.device_id)

    dev_reg = dr.async_get(hass)
    for device_id in device_ids:
        device = dev_reg.async_get(device_id)
        if device:
            entries.update(device.config_entries)

    known = hass.data.get(DOMAIN) or {}
    return {
        entry_id: known[entry_id]["coordinator"]
        for entry_id in entries
        if entry_id in known and "coordinator" in known[entry_id]
    }


def _require_targets(
    hass: HomeAssistant, call: ServiceCall
) -> dict[str, TaipowerDataUpdateCoordinator]:
    coordinators = _resolve_coordinators(hass, call)
    if not coordinators:
        raise HomeAssistantError(
            "找不到 taipower_bill 的目標：請用 target: 指定裝置／實體，"
            "或直接帶 entry_id。"
        )
    return coordinators


async def _handle_close_period(hass: HomeAssistant, call: ServiceCall) -> dict:
    results: dict[str, Any] = {}
    for entry_id, coordinator in _require_targets(hass, call).items():
        results[entry_id] = await coordinator.async_close_period(
            actual_kwh=call.data.get("actual_kwh"),
            actual_bill=call.data.get("actual_bill"),
            force=bool(call.data.get("force", False)),
        )
    return {"closed": results}


async def _handle_set_last_read_date(hass: HomeAssistant, call: ServiceCall) -> dict:
    wanted = parse_date(call.data.get("date"))
    if wanted is None:
        raise HomeAssistantError("date 必須是 YYYY-MM-DD 格式的日期。")
    reset_history = bool(call.data.get("reset_history", False))
    results: dict[str, Any] = {}
    for entry_id, coordinator in _require_targets(hass, call).items():
        results[entry_id] = await coordinator.async_set_last_read_date(
            wanted, reset_history=reset_history
        )
    return {"updated": results}


async def _handle_reset_ledger(hass: HomeAssistant, call: ServiceCall) -> dict:
    clear_baseline = bool(call.data.get("clear_baseline", True))
    results: dict[str, Any] = {}
    for entry_id, coordinator in _require_targets(hass, call).items():
        results[entry_id] = await coordinator.async_reset_ledger(clear_baseline=clear_baseline)
    return {"reset": results}


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the recovery/repair services once per HA instance."""
    if hass.services.has_service(DOMAIN, SERVICE_CLOSE_PERIOD):
        return
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLOSE_PERIOD,
        _handle_close_period,
        schema=CLOSE_PERIOD_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_LAST_READ,
        _handle_set_last_read_date,
        schema=SET_LAST_READ_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_LEDGER,
        _handle_reset_ledger,
        schema=RESET_LEDGER_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Taipower Bill from a config entry (one entry = one 電表/戶)."""
    coordinator = TaipowerDataUpdateCoordinator(hass, entry)
    await coordinator.ledger.async_load()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "manual_kwh": None,
        "actual_bill": None,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _async_register_services(hass)

    _LOGGER.info(
        "Taipower Bill '%s' ready: 上期抄見日 %s, 本期 %s → %s, 來源模式 %s%s",
        coordinator.label,
        coordinator.last_read_date,
        coordinator.period_start,
        coordinator.period_end,
        coordinator.source_mode,
        f" ⚠ {coordinator._config_warning()}" if coordinator.config_issues else "",
    )
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options (抄見日 / 費率設定) change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
