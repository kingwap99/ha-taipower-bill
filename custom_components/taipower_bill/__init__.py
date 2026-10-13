"""Set up the Taipower Bill integration.

The services are deliberately split into "propose" and "write" halves.  Home
Assistant has no native confirmation step for services (only Lovelace's
``tap_action: confirmation:``, which does nothing for an automation or a script),
so the safety has to live here:

* ``close_period``                -- *proposes* a 結算 (nothing is written).
* ``confirm_close`` / ``cancel_close`` -- accept or drop that proposal.
* ``close_period`` with ``confirm: true`` -- propose + write in one call, which
  is what an automation wants.

Everything else repairs the ledger after the fact, because a mis-press must
never be final: ``add_reading`` (write a 抄見日 straight, including a future
下次抄表日), ``update_reading`` (rewrite one), ``undo_close_period`` (remove one)
and ``period_report`` (recompute any window from the recorder on demand).
"""

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
SERVICE_CONFIRM_CLOSE = "confirm_close"
SERVICE_CANCEL_CLOSE = "cancel_close"
SERVICE_ADD_READING = "add_reading"
SERVICE_UNDO_CLOSE = "undo_close_period"
SERVICE_UPDATE_READING = "update_reading"
SERVICE_PERIOD_REPORT = "period_report"
SERVICE_SET_LAST_READ = "set_last_read_date"
SERVICE_RESET_LEDGER = "reset_ledger"

# ``target:`` in a service call is merged into ``call.data`` as
# ``device_id``/``entity_id``, so accepting those keys covers both the YAML
# ``target:`` form and explicit fields.
_TARGET_FIELDS = {
    vol.Optional("entry_id"): vol.Any(None, str),
    vol.Optional("device_id"): vol.Any(None, str, [str]),
    vol.Optional("entity_id"): vol.Any(None, str, [str]),
}

# ``vol.Any(None, ...)`` rather than a bare ``vol.Coerce``: the card sends the
# key with a null value when a field is left empty, and coercing null would
# raise.  A date field keeps ``str`` first so a real ``date`` object from
# Python is accepted as its ISO string.
_NUM = vol.Any(None, vol.Coerce(float))
_DATE = vol.Any(None, vol.Coerce(str))
_BOOL = vol.Any(None, vol.Coerce(bool))

CLOSE_PERIOD_SCHEMA = vol.Schema(
    {
        **_TARGET_FIELDS,
        vol.Optional("read_date"): _DATE,
        vol.Optional("actual_kwh"): _NUM,
        vol.Optional("actual_bill"): _NUM,
        vol.Optional("force", default=False): _BOOL,
        vol.Optional("confirm", default=False): _BOOL,
    }
)

CONFIRM_CLOSE_SCHEMA = vol.Schema({**_TARGET_FIELDS})

CANCEL_CLOSE_SCHEMA = vol.Schema({**_TARGET_FIELDS})

ADD_READING_SCHEMA = vol.Schema(
    {
        **_TARGET_FIELDS,
        vol.Required("date"): vol.Coerce(str),
        vol.Optional("actual_kwh"): _NUM,
        vol.Optional("actual_bill"): _NUM,
        vol.Optional("force", default=False): _BOOL,
        vol.Optional("note", default="add_reading"): vol.Any(None, vol.Coerce(str)),
    }
)

UNDO_CLOSE_SCHEMA = vol.Schema(
    {
        **_TARGET_FIELDS,
        vol.Optional("date"): _DATE,
    }
)

UPDATE_READING_SCHEMA = vol.Schema(
    {
        **_TARGET_FIELDS,
        vol.Required("date"): vol.Coerce(str),
        vol.Optional("new_date"): _DATE,
        vol.Optional("actual_kwh"): _NUM,
        vol.Optional("actual_bill"): _NUM,
        vol.Optional("recompute", default=False): _BOOL,
        vol.Optional("clear_amounts", default=False): _BOOL,
    }
)

PERIOD_REPORT_SCHEMA = vol.Schema(
    {
        **_TARGET_FIELDS,
        vol.Optional("date"): _DATE,
        vol.Optional("start"): _DATE,
        vol.Optional("end"): _DATE,
        vol.Optional("all", default=False): _BOOL,
    }
)

SET_LAST_READ_SCHEMA = vol.Schema(
    {
        **_TARGET_FIELDS,
        vol.Required("date"): vol.Coerce(str),
        vol.Optional("reset_history", default=False): _BOOL,
    }
)

RESET_LEDGER_SCHEMA = vol.Schema(
    {
        **_TARGET_FIELDS,
        vol.Optional("clear_readings", default=True): _BOOL,
        vol.Optional("clear_baseline", default=True): _BOOL,
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
    """Map a service target (device/entity/entry_id) to our coordinators."""
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


def _required_date(value: Any, field: str):
    parsed = parse_date(value)
    if parsed is None:
        raise HomeAssistantError(f"{field} 必須是 YYYY-MM-DD 格式的日期（收到 {value!r}）。")
    return parsed


def _optional_date(value: Any, field: str):
    if value in (None, ""):
        return None
    return _required_date(value, field)


async def _handle_close_period(hass: HomeAssistant, call: ServiceCall) -> dict:
    """Propose a 結算 -- or write it, when ``confirm`` is true."""
    results: dict[str, Any] = {}
    for entry_id, coordinator in _require_targets(hass, call).items():
        results[entry_id] = await coordinator.async_close_period(
            read_date=_optional_date(call.data.get("read_date"), "read_date"),
            actual_kwh=call.data.get("actual_kwh"),
            actual_bill=call.data.get("actual_bill"),
            force=bool(call.data.get("force", False)),
            confirm=bool(call.data.get("confirm", False)),
        )
    return {"close_period": results}


async def _handle_confirm_close(hass: HomeAssistant, call: ServiceCall) -> dict:
    results: dict[str, Any] = {}
    for entry_id, coordinator in _require_targets(hass, call).items():
        results[entry_id] = await coordinator.async_confirm_close()
    return {"confirmed": results}


async def _handle_cancel_close(hass: HomeAssistant, call: ServiceCall) -> dict:
    results: dict[str, Any] = {}
    for entry_id, coordinator in _require_targets(hass, call).items():
        results[entry_id] = await coordinator.async_cancel_close()
    return {"cancelled": results}


async def _handle_add_reading(hass: HomeAssistant, call: ServiceCall) -> dict:
    wanted = _required_date(call.data.get("date"), "date")
    results: dict[str, Any] = {}
    for entry_id, coordinator in _require_targets(hass, call).items():
        results[entry_id] = await coordinator.async_add_reading(
            wanted,
            actual_kwh=call.data.get("actual_kwh"),
            actual_bill=call.data.get("actual_bill"),
            force=bool(call.data.get("force", False)),
            note=str(call.data.get("note") or "add_reading"),
        )
    return {"add_reading": results}


async def _handle_undo_close_period(hass: HomeAssistant, call: ServiceCall) -> dict:
    when = _optional_date(call.data.get("date"), "date")
    results: dict[str, Any] = {}
    for entry_id, coordinator in _require_targets(hass, call).items():
        results[entry_id] = await coordinator.async_undo_close_period(when)
    return {"undo_close_period": results}


async def _handle_update_reading(hass: HomeAssistant, call: ServiceCall) -> dict:
    when = _required_date(call.data.get("date"), "date")
    results: dict[str, Any] = {}
    for entry_id, coordinator in _require_targets(hass, call).items():
        results[entry_id] = await coordinator.async_update_reading(
            when,
            new_date=_optional_date(call.data.get("new_date"), "new_date"),
            actual_kwh=call.data.get("actual_kwh"),
            actual_bill=call.data.get("actual_bill"),
            recompute=bool(call.data.get("recompute", False)),
            clear_amounts=bool(call.data.get("clear_amounts", False)),
        )
    return {"update_reading": results}


async def _handle_period_report(hass: HomeAssistant, call: ServiceCall) -> dict:
    results: dict[str, Any] = {}
    for entry_id, coordinator in _require_targets(hass, call).items():
        results[entry_id] = await coordinator.async_period_report(
            when=_optional_date(call.data.get("date"), "date"),
            start=_optional_date(call.data.get("start"), "start"),
            end=_optional_date(call.data.get("end"), "end"),
            all_periods=bool(call.data.get("all", False)),
        )
    return {"period_report": results}


async def _handle_set_last_read_date(hass: HomeAssistant, call: ServiceCall) -> dict:
    wanted = _required_date(call.data.get("date"), "date")
    reset_history = bool(call.data.get("reset_history", False))
    results: dict[str, Any] = {}
    for entry_id, coordinator in _require_targets(hass, call).items():
        results[entry_id] = await coordinator.async_set_last_read_date(
            wanted, reset_history=reset_history
        )
    return {"set_last_read_date": results}


async def _handle_reset_ledger(hass: HomeAssistant, call: ServiceCall) -> dict:
    results: dict[str, Any] = {}
    for entry_id, coordinator in _require_targets(hass, call).items():
        results[entry_id] = await coordinator.async_reset_ledger(
            clear_readings=bool(call.data.get("clear_readings", True)),
            clear_baseline=bool(call.data.get("clear_baseline", True)),
        )
    return {"reset_ledger": results}


SERVICES: tuple[tuple[str, Any, Any], ...] = (
    (SERVICE_CLOSE_PERIOD, _handle_close_period, CLOSE_PERIOD_SCHEMA),
    (SERVICE_CONFIRM_CLOSE, _handle_confirm_close, CONFIRM_CLOSE_SCHEMA),
    (SERVICE_CANCEL_CLOSE, _handle_cancel_close, CANCEL_CLOSE_SCHEMA),
    (SERVICE_ADD_READING, _handle_add_reading, ADD_READING_SCHEMA),
    (SERVICE_UNDO_CLOSE, _handle_undo_close_period, UNDO_CLOSE_SCHEMA),
    (SERVICE_UPDATE_READING, _handle_update_reading, UPDATE_READING_SCHEMA),
    (SERVICE_PERIOD_REPORT, _handle_period_report, PERIOD_REPORT_SCHEMA),
    (SERVICE_SET_LAST_READ, _handle_set_last_read_date, SET_LAST_READ_SCHEMA),
    (SERVICE_RESET_LEDGER, _handle_reset_ledger, RESET_LEDGER_SCHEMA),
)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the ledger services once per HA instance."""
    if hass.services.has_service(DOMAIN, SERVICE_CLOSE_PERIOD):
        return
    for name, handler, schema in SERVICES:
        hass.services.async_register(
            DOMAIN,
            name,
            handler,
            schema=schema,
            supports_response=SupportsResponse.OPTIONAL,
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Taipower Bill from a config entry (one entry = one 電表/戶)."""
    coordinator = TaipowerDataUpdateCoordinator(hass, entry)
    # The read-date ledger has to be loaded *before* the first refresh: it is
    # what the whole period window is derived from.
    await coordinator.async_load_ledger()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "manual_kwh": None,
        "actual_bill": None,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _async_register_services(hass)

    # Back-filling and publishing touch the recorder, so keep them off the
    # setup path: a slow or broken statistics source must not delay the
    # entities coming up.
    if hasattr(entry, "async_create_background_task"):
        entry.async_create_background_task(
            hass, coordinator.async_prepare(), name=f"{DOMAIN}_prepare"
        )
    else:  # pragma: no cover - older core
        hass.async_create_task(coordinator.async_prepare())

    _LOGGER.info(
        "Taipower Bill '%s' ready: 抄見日 %s, 本期 %s → %s, 來源模式 %s%s",
        coordinator.label,
        [r.get("date") for r in coordinator.readings] or "（空）",
        coordinator.period_start,
        coordinator.period_end,
        coordinator.source_mode,
        f" ⚠ {coordinator._config_warning()}" if coordinator.config_issues else "",
    )
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options (費率設定) change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
