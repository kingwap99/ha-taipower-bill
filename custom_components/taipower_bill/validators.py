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
CONF_NOTIFY_TARGETS = "notify_targets"

SOURCE_MODE_STATISTICS = "statistics"
SOURCE_MODE_MANUAL = "manual"

DEFAULT_CYCLE_DAYS = 60
#: 台電 reads bimonthly; anything much shorter than this is a typo.
MIN_CYCLE_DAYS = 20
#: 上期抄見日 older than this is almost certainly a wrong year.
MAX_LOOKBACK_DAYS = 400

#: The confirmation buttons on a 結算 notification.
#:
#: The Companion App reports a button press as a single string on
#: ``event.data.action`` -- it sends no service call back into the integration,
#: and none of the other keys of the notification come back with it.  The
#: identifier therefore has to carry everything the handler needs, which is why
#: the ``entry_id`` is baked into the string.  Home Assistant core does exactly
#: the same thing in its own ``confirmable_notification`` script blueprint,
#: which builds ``CONFIRM_<context.id>`` and then waits for the event:
#:
#:   homeassistant/components/script/blueprints/confirmable_notification.yaml
#:     - action: "{{ action_confirm }}"
#:     ...
#:     event_type: mobile_app_notification_action
#:     event_data:
#:       action: "{{ action_confirm }}"
#:
#: ``mobile_app_notification_action`` is emitted by the *app*, so it is not
#: defined anywhere in core -- the blueprint above is the in-tree reference for
#: its shape.
NOTIFY_ACTION_PREFIX = "TAIPOWER_"

ACTION_CONFIRM = "CONFIRM"
ACTION_CANCEL = "CANCEL"
ACTION_UNDO = "UNDO"

#: Which buttons mean what.  ``UNDO`` is not part of a proposal -- it is offered
#: on the *follow-up* notification, so that a 結算 confirmed by a stray tap can
#: still be taken back from the same phone.
NOTIFY_ACTION_KINDS = (ACTION_CONFIRM, ACTION_CANCEL, ACTION_UNDO)


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


def notify_action(kind: str, entry_id: str) -> str:
    """Build the identifier for one notification button."""
    return f"{NOTIFY_ACTION_PREFIX}{kind}_{entry_id}"


def parse_notify_action(value: Any) -> tuple[str, str] | None:
    """``("CONFIRM", entry_id)`` from a ``mobile_app_notification_action`` payload.

    ``None`` for anything that is not one of *our* actions.  That event type is
    shared by every integration and by hand-written automations, so an
    unrecognised string has to be ignored rather than guessed at -- acting on
    somebody else's ``CONFIRM_...`` would write to the ledger.

    The split is on the *first* underscore, so an ``entry_id`` that contains one
    (ours are ULIDs and do not, but a hand-edited one might) still parses.
    """
    if not isinstance(value, str) or not value.startswith(NOTIFY_ACTION_PREFIX):
        return None
    kind, _, entry_id = value[len(NOTIFY_ACTION_PREFIX) :].partition("_")
    if kind not in NOTIFY_ACTION_KINDS or not entry_id:
        return None
    return kind, entry_id


def normalise_notify_target(value: Any) -> tuple[str, str] | None:
    """``"notify.mobile_app_x"`` or ``"mobile_app_x"`` -> ``("notify", "mobile_app_x")``.

    Stored options and hand-written YAML differ on whether the domain is
    included; accept both so a user cannot silently end up with a notification
    that never fires.
    """
    if not isinstance(value, str):
        return None
    parts = value.strip().split(".")
    if not parts or not all(parts):
        return None
    if len(parts) == 1:
        return "notify", parts[0]
    if len(parts) == 2:
        return parts[0], parts[1]
    return None


def normalise_notify_targets(value: Any) -> list[tuple[str, str]]:
    """Normalise the whole ``notify_targets`` option into ``(domain, service)`` pairs."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    out: list[tuple[str, str]] = []
    for item in items:
        norm = normalise_notify_target(item)
        if norm is not None and norm not in out:
            out.append(norm)
    return out
