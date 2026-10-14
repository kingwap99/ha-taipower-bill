"""The 結算 notification buttons and the 「撤銷最後一期」 entity, on a real HA.

This is the one path that cannot be stubbed.  A notification button press reaches
an integration as a ``mobile_app_notification_action`` **event** on the shared
bus -- never as a service call, and with none of the notification's other keys
attached.  So the design under test is exactly: the action string carries the
entry id, the integration recognises its own actions and ignores everybody
else's, and a button that cannot do anything says so instead of going quiet.

The Companion App itself cannot be installed here, so the ``notify`` service it
would register is registered by hand and its payload captured; every step
downstream of that is the real integration.  (``persistent_notification`` is not
a substitute: in HA 2026.10 its ``async_create`` takes only
message/title/notification_id, and the ``Notification`` TypedDict has no
``actions`` key -- there is no way for core notifications to carry buttons.)

Time is frozen, so the windows and the expected dates are deterministic:
異動日 2026-09-07 + 60 天 ⇒ 本期 2026-09-08 → 2026-11-06.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

DOMAIN = "taipower_bill"
EVENT = "mobile_app_notification_action"
TODAY = datetime(2026, 10, 14, 20, 0, tzinfo=UTC)  # 13:00 US/Pacific (the harness tz)

BASE = {
    "label": "A戶",
    "source_entities": ["sensor.house_a_energy_this_month"],
    "source_mode": "manual",
    "cycle_days": 60,
    "last_read_date": "2026-09-07",
}


def _register_notify(hass: HomeAssistant) -> list[dict]:
    """Stand in for the Companion App's ``notify.mobile_app_*`` service."""
    calls: list[dict] = []

    async def _capture(call) -> None:
        calls.append(dict(call.data))

    hass.services.async_register("notify", "mobile_app_test", _capture)
    return calls


async def _setup(hass: HomeAssistant, *, push: bool = True, options: dict | None = None):
    hass.states.async_set("sensor.house_a_energy_this_month", "123.4")
    opts = dict(BASE)
    if push:
        opts["notify_targets"] = ["notify.mobile_app_test"]
    if options:
        opts.update(options)
    entry = MockConfigEntry(
        domain=DOMAIN, title="A戶", options=opts, unique_id="taipower_bill.a"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state.value == "loaded"
    return entry


async def _settle(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=90))
    await hass.async_block_till_done()


def _s(hass: HomeAssistant, eid: str):
    st = hass.states.get(eid)
    return None if st is None else st.state


def _attrs(hass: HomeAssistant, eid: str) -> dict:
    return dict(hass.states.get(eid).attributes)


async def _manual(hass: HomeAssistant, kwh: float) -> None:
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.tai_dian_ahu_period_usage_manual", "value": kwh},
        blocking=True,
    )
    await _settle(hass)


async def _call(hass: HomeAssistant, service: str, data: dict):
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )


async def _propose(hass: HomeAssistant, entry) -> None:
    """Propose a 結算 that is earlier than the projection -- hence ``force``."""
    await _manual(hass, 754.633)
    await _call(
        hass,
        "close_period",
        {"entry_id": entry.entry_id, "read_date": "2026-10-05", "force": True},
    )
    await _settle(hass)


async def _press(hass: HomeAssistant, action: str) -> None:
    """Do what the app does: fire the event, touching no service ourselves."""
    hass.bus.async_fire(EVENT, {"action": action})
    await hass.async_block_till_done()
    await _settle(hass)


def _actions(payload: dict) -> dict[str, str]:
    return {a["action"]: a["title"] for a in (payload.get("data", {}).get("actions") or [])}


def _with_actions(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c.get("data", {}).get("actions")]


@freeze_time(TODAY)
async def test_the_proposal_notification_carries_confirm_and_cancel(
    hass: HomeAssistant,
) -> None:
    """A proposal also goes to the phone, with both buttons wired to this entry."""
    calls = _register_notify(hass)
    entry = await _setup(hass)
    await _propose(hass, entry)

    actionable = _with_actions(calls)
    print("\n=== notifications:", calls)
    assert actionable, "no notification with actions was sent"

    payload = actionable[-1]
    assert _actions(payload) == {
        f"TAIPOWER_CONFIRM_{entry.entry_id}": "確認寫入",
        f"TAIPOWER_CANCEL_{entry.entry_id}": "取消",
    }
    assert "待確認結算" in payload["title"]
    # One tag per 戶, so every later message (已結算／已撤銷／操作未完成) *replaces*
    # this one instead of leaving a stale proposal on the lock screen whose
    # buttons can no longer work.
    assert payload["data"]["tag"] == f"{DOMAIN}_{entry.entry_id}"
    assert payload["data"]["group"] == f"{DOMAIN}_{entry.entry_id}"
    # 「一下子就不見了」: a notification nobody can read in time is useless, so
    # the payload asks the OS to keep it (in-tree shape: mobile_app/timers.py).
    assert payload["data"]["push"]["interruption-level"] == "time-sensitive"
    assert payload["data"]["importance"] == "high"
    assert payload["data"]["ttl"] == 0
    # ...and nothing has been written yet: this is still only a proposal.
    assert _s(hass, "sensor.tai_dian_ahu_closed_periods") == "0"


@freeze_time(TODAY)
async def test_the_phone_confirm_button_writes_the_reading(hass: HomeAssistant) -> None:
    """Pressing 確認 on the lock screen is what commits the period."""
    calls = _register_notify(hass)
    entry = await _setup(hass)
    await _propose(hass, entry)
    assert _s(hass, "sensor.tai_dian_ahu_closed_periods") == "0"

    await _press(hass, f"TAIPOWER_CONFIRM_{entry.entry_id}")

    print("\n=== closed_periods:", _s(hass, "sensor.tai_dian_ahu_closed_periods"))
    assert _s(hass, "sensor.tai_dian_ahu_closed_periods") == "1"
    assert (
        _attrs(hass, "sensor.tai_dian_ahu_current_period_usage")["period_start"]
        == "2026-10-06"
    )

    # and it answers with a 撤銷 button -- the point being that a stray tap on
    # 確認 is recoverable from the very same notification.
    undo = [
        a
        for call in _with_actions(calls)
        for a in call["data"]["actions"]
        if a["action"] == f"TAIPOWER_UNDO_{entry.entry_id}"
    ]
    assert undo, f"no 撤銷 button was offered after the close: {calls}"
    assert undo[-1]["title"] == "撤銷這一期"
    # ...and it lands on the *same* tag as the proposal, so the phone shows one
    # current 台電 notification per household rather than a stale one.
    assert {c["data"]["tag"] for c in _with_actions(calls)} == {f"{DOMAIN}_{entry.entry_id}"}


@freeze_time(TODAY)
async def test_the_phone_undo_button_takes_it_back(hass: HomeAssistant) -> None:
    """The rescue path: undo the write without opening Home Assistant."""
    _register_notify(hass)
    entry = await _setup(hass)
    await _propose(hass, entry)
    await _call(hass, "confirm_close", {"entry_id": entry.entry_id})
    await _settle(hass)
    assert _s(hass, "sensor.tai_dian_ahu_closed_periods") == "1"

    await _press(hass, f"TAIPOWER_UNDO_{entry.entry_id}")

    print("\n=== after undo:", _s(hass, "sensor.tai_dian_ahu_closed_periods"))
    assert _s(hass, "sensor.tai_dian_ahu_closed_periods") == "0"
    assert (
        _attrs(hass, "sensor.tai_dian_ahu_current_period_usage")["period_start"]
        == "2026-09-08"
    )


@freeze_time(TODAY)
async def test_everybody_elses_actions_are_ignored(hass: HomeAssistant) -> None:
    """The event is shared.  A stray ``CONFIRM_…`` must not touch our ledger.

    Home Assistant's own confirmable-notification blueprint fires
    ``CONFIRM_<context.id>`` on this same event type, and so does any hand-written
    automation, so the handler has to recognise its own actions and drop the rest.
    """
    _register_notify(hass)
    entry = await _setup(hass)
    await _propose(hass, entry)

    for action in (
        "CONFIRM_abc",  # the core blueprint's shape
        "DISMISS_abc",
        "TAIPOWER_",
        "TAIPOWER_NOPE_" + entry.entry_id,  # our prefix, unknown kind
        "TAIPOWER_CONFIRM_",  # our prefix, no entry
        "TAIPOWER_CONFIRM_01ZZZZZZZZZZZZZZZZZZZZZZZZ",  # valid shape, unknown entry
        "",
    ):
        await _press(hass, action)
    hass.bus.async_fire(EVENT, {})  # no action key at all
    await hass.async_block_till_done()

    assert _s(hass, "sensor.tai_dian_ahu_closed_periods") == "0"
    # the proposal is untouched and still waiting
    assert _s(hass, "sensor.tai_dian_ahu_pending_meter_read_date") == "2026-10-05"


@freeze_time(TODAY)
async def test_a_button_that_cannot_work_says_so(hass: HomeAssistant) -> None:
    """A press that does nothing must not do it silently.

    Here there is no proposal at all (the obvious real case is an expired one:
    the notification is still on the lock screen after ``PENDING_TTL``), so
    ``confirm_close`` raises -- and the user has to be told, on the same channel
    they just pressed.
    """
    calls = _register_notify(hass)
    entry = await _setup(hass)
    calls.clear()

    await _press(hass, f"TAIPOWER_CONFIRM_{entry.entry_id}")

    print("\n=== failure notification:", calls)
    answered = [c for c in calls if "沒有生效" in str(c.get("message"))]
    assert answered, f"the dead button went silent: {calls}"
    assert "待確認的結算" in answered[0]["message"]
    # the answer replaces the dead notification: same tag, and no buttons on it
    assert answered[0]["data"]["tag"] == f"{DOMAIN}_{entry.entry_id}"
    assert "actions" not in answered[0]["data"]
    assert _s(hass, "sensor.tai_dian_ahu_closed_periods") == "0"


@freeze_time(TODAY)
async def test_no_push_target_means_no_push(hass: HomeAssistant) -> None:
    """The default is silence: the option is opt-in, and a 結算 still works."""
    calls = _register_notify(hass)
    entry = await _setup(hass, push=False)
    await _propose(hass, entry)

    assert calls == [], calls
    assert _s(hass, "sensor.tai_dian_ahu_pending_meter_read_date") == "2026-10-05"


@freeze_time(TODAY)
async def test_the_undo_button_entity(hass: HomeAssistant) -> None:
    """A real button entity for the mis-tap rescue, safe when there is nothing to do."""
    _register_notify(hass)
    entry = await _setup(hass)
    reg = er.async_get(hass)
    eid = reg.async_get_entity_id(
        "button", DOMAIN, f"{DOMAIN}.{entry.entry_id}.undo_close_period"
    )
    assert eid, "the 撤銷 button is not registered"
    print(f"\n=== {eid} = {_s(hass, eid)} {_attrs(hass, eid)}")

    # 抄見日 2026-09-07 is in the past, so there *is* a period to take back --
    # and the button names it, because a button entity cannot ask "are you sure?".
    assert _s(hass, eid) != "unavailable"
    assert _attrs(hass, eid)["will_remove"] == "2026-09-07"
    assert _attrs(hass, eid)["can_undo"] is True

    # With a proposal open the meaningful action is 取消結算, not 撤銷 -- so it
    # goes unavailable rather than looking like it will do something.
    await _manual(hass, 754.633)
    await _call(
        hass,
        "close_period",
        {"entry_id": entry.entry_id, "read_date": "2026-10-05", "force": True},
    )
    await _settle(hass)
    assert _s(hass, eid) == "unavailable"

    await _call(hass, "cancel_close", {"entry_id": entry.entry_id})
    await _settle(hass)
    assert _s(hass, eid) != "unavailable"

    # close for real, then press the button
    await _call(
        hass,
        "close_period",
        {
            "entry_id": entry.entry_id,
            "read_date": "2026-10-05",
            "force": True,
            "confirm": True,
        },
    )
    await _settle(hass)
    assert _s(hass, "sensor.tai_dian_ahu_closed_periods") == "1"
    assert _attrs(hass, eid)["will_remove"] == "2026-10-05"

    await hass.services.async_call(
        "button", "press", {"entity_id": eid}, blocking=True
    )
    await _settle(hass)
    print("=== after press:", _s(hass, "sensor.tai_dian_ahu_closed_periods"))
    assert _s(hass, "sensor.tai_dian_ahu_closed_periods") == "0"
    assert (
        _attrs(hass, "sensor.tai_dian_ahu_current_period_usage")["period_start"]
        == "2026-09-08"
    )

    # empty ledger -> nothing to undo, so unavailable rather than a dead button.
    # (HA publishes no attributes at all for an unavailable entity, so
    # ``will_remove``/``can_undo`` are gone here -- that is core's behaviour, not
    # a missing attribute.)
    await _call(hass, "reset_ledger", {"entry_id": entry.entry_id})
    await _settle(hass)
    assert _s(hass, eid) == "unavailable"
    assert "will_remove" not in _attrs(hass, eid)


async def test_the_options_flow_offers_the_notify_targets(hass: HomeAssistant) -> None:
    """選項裡要能挑手機，而且挑完要存得住（否則通知永遠不會發）。"""

    async def _noop(call) -> None:
        return None

    hass.services.async_register("notify", "mobile_app_test_phone", _noop)

    entry = MockConfigEntry(
        domain=DOMAIN, title="A戶", options=dict(BASE), unique_id="taipower_bill.a"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    keys = [str(k) for k in result["data_schema"].schema]
    print("\n=== options schema keys:", keys)
    assert "notify_targets" in keys

    # the selector lists the app's own services, because only those render buttons
    selector = result["data_schema"].schema["notify_targets"]
    options = list(selector.config["options"])
    print("=== notify options:", options)
    assert "notify.mobile_app_test_phone" in options

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"notify_targets": ["notify.mobile_app_test_phone"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["notify_targets"] == ["notify.mobile_app_test_phone"]
