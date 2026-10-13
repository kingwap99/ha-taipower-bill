"""Pure-Python validation shared by the config flow, the services and the coordinator.

Deliberately free of Home Assistant imports so it can be unit-tested standalone
(see ``tests/test_billing.py``) -- the bug this module exists to prevent was a
config value that only ever got validated by a live HA instance.

The config key names it needs live here too, so this module is the single source
of truth for them; ``const.py`` re-exports them for the rest of the integration.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

CONF_LABEL = "label"
CONF_SOURCE_ENTITIES = "source_entities"
CONF_SOURCE_MODE = "source_mode"
CONF_LAST_READ_DATE = "last_read_date"
CONF_CYCLE_DAYS = "cycle_days"

SOURCE_MODE_STATISTICS = "statistics"
SOURCE_MODE_MANUAL = "manual"

DEFAULT_CYCLE_DAYS = 60
#: 台電 reads bimonthly; anything much shorter than this is a typo.
MIN_CYCLE_DAYS = 20
#: 上期抄見日 older than this is almost certainly a wrong year.
MAX_LOOKBACK_DAYS = 400


def parse_date(value: Any) -> date | None:
    """Accept what a DateSelector, a service call, or stored JSON hands us."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
    return None


def validate_basics(user_input: dict[str, Any], today: date) -> dict[str, str]:
    """Validate the 戶別 fields, shared by the create *and* reconfigure steps.

    Returns a ``{field: error_key}`` mapping, empty when everything is fine.

    The 上期抄見日 check is the important one.  Nothing used to validate it, so an
    accidental extra press of 「結算本期」 (which advances 抄見日 by ``cycle_days``
    each time) could be saved, pushing the whole current period window into the
    future.  Every derived sensor then had a start-after-end window to work with
    and went blank -- which is exactly what "the values are almost all empty"
    looked like from the outside.
    """
    errors: dict[str, str] = {}

    label = (user_input.get(CONF_LABEL) or "").strip()
    if not label:
        errors[CONF_LABEL] = "label_required"

    if user_input.get(CONF_SOURCE_MODE) == SOURCE_MODE_STATISTICS and not user_input.get(
        CONF_SOURCE_ENTITIES
    ):
        errors[CONF_SOURCE_ENTITIES] = "source_required"

    parsed = parse_date(user_input.get(CONF_LAST_READ_DATE))
    if parsed is None:
        errors[CONF_LAST_READ_DATE] = "last_read_invalid"
    elif parsed > today:
        errors[CONF_LAST_READ_DATE] = "last_read_in_future"
    elif (today - parsed).days > MAX_LOOKBACK_DAYS:
        errors[CONF_LAST_READ_DATE] = "last_read_too_old"

    raw_cycle = user_input.get(CONF_CYCLE_DAYS)
    if raw_cycle is not None:
        try:
            if float(raw_cycle) < MIN_CYCLE_DAYS:
                errors[CONF_CYCLE_DAYS] = "cycle_days_too_short"
        except (TypeError, ValueError):
            errors[CONF_CYCLE_DAYS] = "cycle_days_invalid"

    return errors
