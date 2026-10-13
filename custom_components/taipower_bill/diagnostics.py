"""Diagnostics dump for troubleshooting a Taipower Bill entry.

This is the fastest way to see the real state of an entry without reading its
config files: it shows the read-date list (the source of truth), why the last
period could not be computed, whether the external statistics write succeeded,
and the exact statistic ids the series are published under.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .tariffs import REVISIONS


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    data = coordinator.data or {}
    bill = data.get("bill")
    kwh_id, cost_id = coordinator.statistic_ids()
    readings = coordinator.readings
    windows = coordinator.period_windows()

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
            "readings": [r.get("date") for r in readings],
            "last_read_date": _iso(coordinator.last_read_date),
            "next_read_date": _iso(coordinator.next_read_date),
            "period_start": _iso(coordinator.period_start),
            "period_end": _iso(coordinator.period_end),
            "suggested_read_date": _iso(coordinator.suggested_read_date),
            "period_has_started": coordinator.period_has_started,
            "period_has_ended": coordinator.period_has_ended,
            "days_elapsed": data.get("days_elapsed"),
            "days_in_period": data.get("days_in_period"),
            "days_remaining": data.get("days_remaining"),
            "usage_source": data.get("usage_source"),
            "stats_coverage_start": _iso(data.get("stats_coverage_start")),
            "stats_last_day": _iso(data.get("stats_last_day")),
            "stats_error": data.get("stats_error"),
            "stats_write_error": data.get("stats_write_error"),
        },
        "config_issues": data.get("config_issues") or [],
        "config_warning": data.get("config_warning"),
        "pending_close": data.get("pending_close"),
        "ledger": {
            # Full rows, including each period's cached computation.
            "readings": readings,
            "cached_periods": sum(
                1 for r in readings if (r.get("computed") or {}).get("kwh") is not None
            ),
            "baseline_kwh": coordinator.ledger.data.get("baseline_kwh"),
            "baseline_date": coordinator.ledger.data.get("baseline_date"),
        },
        "period_windows": [
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "days": (end - start).days + 1,
                "kwh": (current.get("computed") or {}).get("kwh"),
                "actual_kwh": current.get("kwh"),
                "actual_bill": current.get("bill"),
                "forced": bool(current.get("forced")),
                "closed": end <= coordinator.today,
            }
            for start, end, _previous, current in windows
        ],
        "external_statistics": {
            "statistic_ids": [kwh_id, cost_id],
            # Periods that are finished *and* cached, i.e. what the series holds.
            "published_periods": len(coordinator._statistic_rows()[0]),
            "unit_class_kwh": "energy",
            "unit_class_cost": None,
        },
        "bill": bill.as_attributes() if bill else None,
        "history": data.get("history") or [],
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
