"""Real-HA flows: manual 度數, the tier maths, and the propose/confirm services.

These run against a genuine ``homeassistant`` install (see the repo README), which
is the only way to catch the API drift the stub suites are structurally blind to:
the ``Store`` migration hook, the ``DATE`` sensor's state type, the arity of a
service handler, and the recorder's statistics API.

Time is frozen so the billing window -- and therefore every expected TWD figure
below -- is deterministic.  ``異動日 2026-09-07`` + 60 天 ⇒ 本期 2026-09-08→2026-11-06.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

DOMAIN = "taipower_bill"
TODAY = datetime(2026, 10, 14, 20, 0, tzinfo=UTC)  # 13:00 US/Pacific (the harness tz)
OPTIONS = {
    "label": "A戶",
    "source_entities": ["sensor.house_a_energy_this_month"],
    "source_mode": "manual",
    "cycle_days": 60,
    "last_read_date": "2026-09-07",
}


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    hass.states.async_set("sensor.house_a_energy_this_month", "123.4")
    entry = MockConfigEntry(
        domain=DOMAIN, title="A戶", options=dict(OPTIONS), unique_id="taipower_bill.a_house"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state.value == "loaded"  # a failed setup is why nothing showed up
    return entry


async def _settle(hass: HomeAssistant) -> None:
    """Flush the coordinator's debounced refresh (the number entity debounces)."""
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


def test_entities_exist(hass: HomeAssistant) -> None:
    """23 entities, and the two DATE sensors carry a *date*, not a formatted str.

    The regression this pins down: HA 2026.10 does ``value.isoformat()`` on a
    ``device_class: date`` sensor, so handing it a string raises ValueError and
    the whole platform's states go ``unavailable``.
    """
    from homeassistant.helpers import entity_registry as er

    ent_reg = er.async_get(hass)
    assert ent_reg is not None


@freeze_time(TODAY)
async def test_manual_kwh_flows_into_the_bill(hass: HomeAssistant) -> None:
    await _setup(hass)
    await _manual(hass, 754.633)

    kwh = _s(hass, "sensor.tai_dian_ahu_current_period_usage")
    cost = _s(hass, "sensor.tai_dian_ahu_current_period_cost_estimated")
    a = _attrs(hass, "sensor.tai_dian_ahu_current_period_usage")
    print(f"\n=== 754.633 kWh -> kwh={kwh} cost={cost}")
    print(f"    window {a['bill_window_start']} → {a['bill_window_end']}"
          f" ({a['bill_window_days']} d), days_elapsed={a['days_elapsed']}")

    assert float(kwh) == pytest.approx(754.633, abs=0.001)
    # 2026-09-08 → 2026-10-14 = 37 d = 23 夏月 + 14 非夏月.  The whole bimonthly
    # ladder is priced per season on the TOTAL kWh, then weighted by day share:
    #   夏月 1857.81 × 23/37 + 非夏月 1672.60 × 14/37 = 1787.73
    assert float(cost) == pytest.approx(1787.73, abs=0.05)
    assert a["bill_window_start"] == "2026-09-08"
    assert a["bill_window_end"] == "2026-10-14"

    # the tier breakdown is the ladder, never split across the seasons
    tiers = a["tiers"]
    assert [(t["up_to_kwh"], t["price"]) for t in tiers] == [
        (240, 1.78), (660, 2.55), (1000, 3.8)
    ]
    assert sum(t["kwh"] for t in tiers) == pytest.approx(754.633, abs=0.001)

    # 抄見日 = 2026-09-07, so the next projected read is exactly +60 d
    assert _s(hass, "sensor.tai_dian_ahu_next_projected_read_date") == "2026-11-06"


@freeze_time(TODAY)
async def test_period_cost_totals_are_a_legal_pair(hass: HomeAssistant) -> None:
    """`total` sensors need a `last_reset`, and their unit pairing must be legal."""
    await _setup(hass)
    await _manual(hass, 754.633)
    a = _attrs(hass, "sensor.tai_dian_ahu_current_period_usage")
    assert a["state_class"] == "total"
    assert a["last_reset"] == "2026-09-08T00:00:00-07:00"
    assert a["unit_of_measurement"] == "kWh"
    # and the *derived* energy sensors must not claim a state class at all
    b = _attrs(hass, "sensor.tai_dian_ahu_average_daily_usage")
    assert "state_class" not in b


@freeze_time(TODAY)
async def test_early_close_is_refused_without_force(hass: HomeAssistant) -> None:
    """A 結算 earlier than the projected 抄見日 needs an explicit ``force``."""
    entry = await _setup(hass)
    await _manual(hass, 754.633)

    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, "close_period",
                    {"entry_id": entry.entry_id, "read_date": "2026-10-05"})
    assert "提前結算" in str(err.value)
    # a refusal must not have written anything
    assert _s(hass, "sensor.tai_dian_ahu_closed_periods") == "0"
    assert _s(hass, "sensor.tai_dian_ahu_pending_meter_read_date") in ("unknown", "None", "")

    # ... and the future-抄見日 case is refused too (this is the v0.1 runaway)
    with pytest.raises(HomeAssistantError) as err2:
        await _call(hass, "close_period",
                    {"entry_id": entry.entry_id, "read_date": "2027-01-31"})
    assert "未來日期" in str(err2.value)


@freeze_time(TODAY)
async def test_propose_confirm_and_undo(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _manual(hass, 754.633)

    # --- propose: nothing is written yet -------------------------------------
    resp = await _call(hass, "close_period",
                       {"entry_id": entry.entry_id, "read_date": "2026-10-05",
                        "force": True})
    await _settle(hass)
    print("\n=== propose response:", resp)
    assert resp["close_period"][entry.entry_id]["pending"] is True
    assert _s(hass, "sensor.tai_dian_ahu_closed_periods") == "0"
    assert _s(hass, "sensor.tai_dian_ahu_pending_meter_read_date") == "2026-10-05"
    assert _s(hass, "button.tai_dian_ahu_confirm_close") != "unavailable"

    # --- confirm: now the reading is committed and the window rolls over -----
    resp2 = await _call(hass, "confirm_close", {"entry_id": entry.entry_id})
    await _settle(hass)
    print("=== confirm response:", resp2, "history:", _s(hass, "sensor.tai_dian_ahu_closed_periods"))
    assert _s(hass, "sensor.tai_dian_ahu_closed_periods") == "1"
    assert _s(hass, "sensor.tai_dian_ahu_previous_period_energy") == "754.633"
    assert _s(hass, "sensor.tai_dian_ahu_pending_meter_read_date") in ("unknown", "None", "")
    # 2026-09-08 → 2026-10-05 = 28 d, still all 夏月 (ends 09-30 … no: 23 夏月 + 5 非夏月)
    last_cost = _s(hass, "sensor.tai_dian_ahu_previous_period_cost")
    assert last_cost not in (None, "unknown")
    assert float(last_cost) > 0
    # the ledger now continues from the day *after* the read date
    a = _attrs(hass, "sensor.tai_dian_ahu_current_period_usage")
    assert a["period_start"] == "2026-10-06"
    assert a["readings"][-1]["date"] == "2026-10-05"

    # --- undo: back where we started ----------------------------------------
    resp3 = await _call(hass, "undo_close_period", {"entry_id": entry.entry_id})
    await _settle(hass)
    print("=== undo response:", resp3)
    assert _s(hass, "sensor.tai_dian_ahu_closed_periods") == "0"
    assert _s(hass, "sensor.tai_dian_ahu_previous_period_energy") in (None, "unknown", "None")
    assert _attrs(hass, "sensor.tai_dian_ahu_current_period_usage")["period_start"] == "2026-09-08"


@freeze_time(TODAY)
async def test_add_reading_accepts_a_future_next_read_date(hass: HomeAssistant) -> None:
    """A future 抄見日 is legitimate *as data* -- it is the 下次抄表日 on the bill."""
    entry = await _setup(hass)
    resp = await _call(hass, "add_reading",
                       {"entry_id": entry.entry_id, "date": "2026-11-06",
                        "actual_kwh": 1200.0})
    await _settle(hass)
    print("\n=== add_reading response:", resp)
    assert resp["add_reading"][entry.entry_id]["readings"]
    assert _s(hass, "sensor.tai_dian_ahu_next_projected_read_date") == "2026-11-06"
    # the current period is unchanged; only the projection moved
    assert _attrs(hass, "sensor.tai_dian_ahu_current_period_usage")["period_start"] == "2026-09-08"


@freeze_time(TODAY)
async def test_period_report_recomputes_an_arbitrary_window(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _manual(hass, 754.633)

    resp = await _call(hass, "period_report",
                       {"entry_id": entry.entry_id,
                        "start": "2026-09-08", "end": "2026-10-14"})
    await _settle(hass)
    rep = resp["period_report"][entry.entry_id]
    print("\n=== period_report response:", rep)
    assert rep["days"] == 37
    # manual mode has no long-term statistics to recompute from, and the current
    # period has no 電費單 度數 yet, so this is the honest "no data" answer rather
    # than a wrong number.  (test_real_statistics.py covers the real path.)
    assert rep["kwh"] is None
    assert "沒有資料" in rep["reason"]

    # `all: true` walks the whole ledger.  A window only exists between two
    # read dates, so a ledger with a single 抄見日 has none.
    resp2 = await _call(hass, "period_report",
                        {"entry_id": entry.entry_id, "all": True})
    assert resp2["period_report"][entry.entry_id]["count"] == 0
    await _call(hass, "add_reading",
                {"entry_id": entry.entry_id, "date": "2026-09-30", "actual_kwh": 500.0})
    await _settle(hass)
    resp3 = await _call(hass, "period_report",
                        {"entry_id": entry.entry_id, "all": True})
    recs = resp3["period_report"][entry.entry_id]["periods"]
    print("=== period_report all:", resp3)
    assert len(recs) == 1
    assert recs[0]["period_start"] == "2026-09-08"
    assert recs[0]["period_end"] == "2026-09-30"

    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, "period_report",
                    {"entry_id": entry.entry_id, "start": "2026-01-01", "end": "2030-01-01"})
    assert "還沒到" in str(err.value)


@freeze_time(TODAY)
async def test_set_last_read_date_and_reset_ledger(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    # Without reset_history the anchor is *inserted*: later read dates are kept,
    # so 「上期抄見日填錯了」 does not throw away periods that were already closed.
    resp = await _call(hass, "set_last_read_date",
                       {"entry_id": entry.entry_id, "date": "2026-07-15"})
    await _settle(hass)
    print("\n=== set_last_read_date:", resp)
    a = _attrs(hass, "sensor.tai_dian_ahu_current_period_usage")
    assert [r["date"] for r in a["readings"]] == ["2026-07-15", "2026-09-07"]
    assert a["period_start"] == "2026-09-08"
    # ... and the new anchor *does* open an earlier window
    assert a["history"][0]["period_start"] == "2026-07-16"
    assert a["history"][0]["period_end"] == "2026-09-07"

    # reset_history replaces the lot
    resp2 = await _call(hass, "set_last_read_date",
                        {"entry_id": entry.entry_id, "date": "2026-07-15",
                         "reset_history": True})
    await _settle(hass)
    print("=== set_last_read_date reset_history:", resp2)
    a = _attrs(hass, "sensor.tai_dian_ahu_current_period_usage")
    assert [r["date"] for r in a["readings"]] == ["2026-07-15"]
    assert a["period_start"] == "2026-07-16"

    resp3 = await _call(hass, "reset_ledger", {"entry_id": entry.entry_id})
    await _settle(hass)
    print("=== reset_ledger:", resp3, "readings:",
          _attrs(hass, "sensor.tai_dian_ahu_current_period_usage")["readings"])
    assert _attrs(hass, "sensor.tai_dian_ahu_current_period_usage")["readings"] == []


@freeze_time(TODAY)
async def test_no_target_is_a_clear_error(hass: HomeAssistant) -> None:
    """Every service must fail with our own message, not a TypeError from HA."""
    await _setup(hass)
    for service, data in (
        ("close_period", {"read_date": "2026-10-05"}),
        ("confirm_close", {}),
        ("cancel_close", {}),
        ("period_report", {"all": True}),
        ("undo_close_period", {}),
        ("reset_ledger", {}),
    ):
        with pytest.raises(HomeAssistantError) as err:
            await _call(hass, service, data)
        assert "找不到" in str(err.value), service
