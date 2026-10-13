"""Diagnostics dump for troubleshooting a Taipower Bill entry."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .tariffs import REVISIONS


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    data = coordinator.data or {}
    bill = data.get("bill")
    return {
        "entry": {
            "title": entry.title,
            "data": dict(entry.data),
            "options": dict(entry.options),
            "source_entities": coordinator.source_entities,
            "source_mode": coordinator.source_mode,
            "cycle_days": coordinator.cycle_days,
            "period_months": coordinator.period_months,
        },
        "period": {
            "last_read_date": coordinator.last_read_date.isoformat(),
            "period_start": coordinator.period_start.isoformat(),
            "period_end": coordinator.period_end.isoformat(),
            "period_has_started": coordinator.period_has_started,
            "period_has_ended": coordinator.period_has_ended,
            "days_elapsed": data.get("days_elapsed"),
            "days_remaining": data.get("days_remaining"),
            "usage_source": data.get("usage_source"),
            "stats_coverage_start": (
                data["stats_coverage_start"].isoformat() if data.get("stats_coverage_start") else None
            ),
        },
        "config_issues": data.get("config_issues") or [],
        "config_warning": data.get("config_warning"),
        "ledger_baseline": {
            "kwh": coordinator.ledger.data.get("baseline_kwh"),
            "date": coordinator.ledger.data.get("baseline_date"),
        },
        "bill": bill.as_attributes() if bill else None,
        "history": coordinator.ledger.periods,
        "tariff_revisions": [
            {
                "key": r.key,
                "effective_from": r.effective_from.isoformat(),
                "monthly_bounds": list(r.monthly_bounds),
                "summer_prices": list(r.summer_prices),
                "off_prices": list(r.off_prices),
                "confirmed": r.confirmed,
                "note": r.note,
            }
            for r in REVISIONS
        ],
        "source_states": {
            eid: (hass.states.get(eid).state if hass.states.get(eid) else "missing")
            for eid in coordinator.source_entities
        },
    }
