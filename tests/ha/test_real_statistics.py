"""Recorder-backed statistics: read, write, and clear.

This is the mode the real 戶 uses (``source_mode: statistics`` on an Emporia
``Energy This Month`` entity), and it is the part a stubbed recorder cannot
check at all: the metadata validity, the row timestamps, the idempotent
re-import, and the fact that ``statistics_during_period`` is a module-level
function that must be dispatched to the executor.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from freezegun import freeze_time
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

DOMAIN = "taipower_bill"
TODAY = datetime(2026, 10, 14, 20, 0, tzinfo=UTC)  # 13:00 US/Pacific (the harness tz)

SOURCE_ID = "emporia:house_a"
PER_DAY = 25.0
WINDOW_START = date(2026, 9, 8)   # the day after the 抄見日
WINDOW_END = date(2026, 10, 13)   # today's daily row is still being written
OPTIONS = {
    "label": "A戶",
    "source_entities": [SOURCE_ID],
    "source_mode": "statistics",
    "cycle_days": 60,
    "last_read_date": "2026-09-07",
}


def _source_rows(start: date, end: date) -> list[dict]:
    """One cumulative daily row per day, exactly how the recorder stores them."""
    rows: list[dict] = []
    running = 0.0
    day = start
    while day <= end:
        running += PER_DAY
        rows.append(
            {
                "start": dt_util.start_of_local_day(day),
                "state": PER_DAY,
                "sum": round(running, 3),
                "last_reset": None,
                "mean": None,
                "min": None,
                "max": None,
            }
        )
        day += timedelta(days=1)
    return rows


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _drain(hass: HomeAssistant, recorder) -> None:
    """Wait for the recorder's queue *and* the coordinator's debounced refresh."""
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    await hass.async_block_till_done()
    await recorder.async_block_till_done()
    # ``async_request_refresh`` is debounced, so the poll only lands once the
    # clock moves.  Time is frozen, so the event has to be fired by hand.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=90))
    await hass.async_block_till_done()
    await recorder.async_block_till_done()
    await hass.async_block_till_done()


def _s(hass: HomeAssistant, eid: str):
    st = hass.states.get(eid)
    return None if st is None else st.state


def _attrs(hass: HomeAssistant, eid: str) -> dict:
    return dict(hass.states.get(eid).attributes)


async def _stat_rows(hass, statistic_id: str, start: date, end: date) -> list[dict]:
    return await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.start_of_local_day(start),
        dt_util.start_of_local_day(end),
        {statistic_id},
        "day",
        None,
        {"change", "sum"},
    )


async def _seed_source(hass: HomeAssistant, recorder) -> None:
    """Feed the recorder the source entity's daily statistics."""
    async_add_external_statistics(
        hass,
        {
            "has_mean": False,
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": "Emporia A 總電",
            "source": "emporia",
            "statistic_id": SOURCE_ID,
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        },
        _source_rows(WINDOW_START, WINDOW_END),
    )
    await _drain(hass, recorder)


@freeze_time(TODAY)
async def test_source_statistics_are_actually_readable(recorder_mock, hass: HomeAssistant) -> None:
    """Sanity-check the fixture itself: the read path the coordinator uses."""
    await _seed_source(hass, recorder_mock)
    rows = await _stat_rows(hass, SOURCE_ID, WINDOW_START, WINDOW_END)
    rows = rows.get(SOURCE_ID) or []
    print(f"\n=== source rows: {len(rows)}")
    assert len(rows) == 36                      # 09-08 … 10-13
    # the recorder derives ``change`` from consecutive ``sum`` values, so the
    # first day of the window is counted even though its own ``sum`` is cumulative
    assert sum(r["change"] for r in rows) == pytest.approx(36 * PER_DAY)
    assert rows[-1]["sum"] == pytest.approx(36 * PER_DAY)


@freeze_time(TODAY)
async def test_statistics_mode_sensors(recorder_mock, hass: HomeAssistant) -> None:
    await _seed_source(hass, recorder_mock)
    entry = MockConfigEntry(
        domain=DOMAIN, title="A戶", options=dict(OPTIONS), unique_id="taipower_bill.a_house"
    )
    await _setup(hass, entry)
    await _drain(hass, recorder_mock)

    a = _attrs(hass, "sensor.tai_dian_ahu_current_period_usage")
    print("\n=== statistics mode:", {k: a[k] for k in (
        "usage_source", "stats_coverage_start", "stats_last_day", "stats_error",
        "kwh", "cost")})
    assert a["usage_source"] == "statistics"
    assert a["stats_error"] is None
    assert a["stats_coverage_start"] == "2026-09-08"
    # Only one 抄見日 (2026-09-07) is on the ledger, so the current window is
    # 2026-09-08 → 2026-10-14.  The daily row for 10-14 is not written yet, so the
    # statistics cover 09-08 … 10-13 = 36 days × 25 kWh.
    assert float(_s(hass, "sensor.tai_dian_ahu_current_period_usage")) == pytest.approx(36 * PER_DAY)
    assert a["bill_window_start"] == "2026-09-08"
    assert a["bill_window_end"] == "2026-10-14"


@freeze_time(TODAY)
async def test_closing_a_period_reads_and_publishes_statistics(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Close a window, then prove the number reached the long-term statistics."""
    await _seed_source(hass, recorder_mock)
    entry = MockConfigEntry(
        domain=DOMAIN, title="A戶", options=dict(OPTIONS), unique_id="taipower_bill.a_house"
    )
    await _setup(hass, entry)
    await _drain(hass, recorder_mock)

    # A past 抄見日 (a real one: the 結算 date on the bill) closes 09-08 → 09-30.
    resp = await hass.services.async_call(
        DOMAIN, "add_reading",
        {"entry_id": entry.entry_id, "date": "2026-09-30"},
        blocking=True, return_response=True,
    )
    await _drain(hass, recorder_mock)
    print("\n=== add_reading:", resp)

    a = _attrs(hass, "sensor.tai_dian_ahu_current_period_usage")
    assert [r["date"] for r in a["readings"]] == ["2026-09-07", "2026-09-30"]
    # 23 days × 25 kWh, and it must have come from the recorder
    assert a["history"][0]["kwh"] == pytest.approx(23 * PER_DAY)
    assert a["history"][0]["source"] == "statistics"
    assert float(_s(hass, "sensor.tai_dian_ahu_previous_period_energy")) == pytest.approx(575.0)
    assert a["stats_write_error"] is None

    # --- the published series -------------------------------------------------
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    kwh_id, cost_id = coordinator.statistic_ids()
    print("=== statistic ids:", kwh_id, cost_id)
    assert kwh_id == "taipower_bill:a_house_period_kwh"

    rows = (await _stat_rows(hass, kwh_id, date(2026, 9, 1), date(2026, 10, 14))).get(kwh_id) or []
    print("=== published kwh rows:", rows)
    assert len(rows) == 1
    # published at local midnight of the day after the 抄見日
    assert rows[0]["sum"] == pytest.approx(575.0)
    assert rows[0]["change"] == pytest.approx(575.0)

    cost_rows = (await _stat_rows(hass, cost_id, date(2026, 9, 1), date(2026, 10, 14))).get(cost_id) or []
    assert len(cost_rows) == 1
    assert cost_rows[0]["sum"] == pytest.approx(a["history"][0]["estimated_total"], abs=0.05)

    # --- re-publishing is idempotent, not a second series ---------------------
    published = await coordinator.async_publish_statistics()
    await _drain(hass, recorder_mock)
    assert published == 1
    rows2 = (await _stat_rows(hass, kwh_id, date(2026, 9, 1), date(2026, 10, 14))).get(kwh_id) or []
    assert len(rows2) == 1, "re-importing must UPDATE the row at the same start"


@freeze_time(TODAY)
async def test_reset_ledger_clears_the_published_statistics(
    recorder_mock, hass: HomeAssistant
) -> None:
    """`async_clear_statistics` moved onto the Recorder instance -- pin it down."""
    await _seed_source(hass, recorder_mock)
    entry = MockConfigEntry(
        domain=DOMAIN, title="A戶", options=dict(OPTIONS), unique_id="taipower_bill.a_house"
    )
    await _setup(hass, entry)
    await hass.services.async_call(
        DOMAIN, "add_reading",
        {"entry_id": entry.entry_id, "date": "2026-09-30"},
        blocking=True, return_response=True,
    )
    await _drain(hass, recorder_mock)

    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    kwh_id, _cost_id = coordinator.statistic_ids()
    before = (await _stat_rows(hass, kwh_id, date(2026, 9, 1), date(2026, 10, 14))).get(kwh_id) or []
    assert before

    resp = await hass.services.async_call(
        DOMAIN, "reset_ledger",
        {"entry_id": entry.entry_id},
        blocking=True, return_response=True,
    )
    await _drain(hass, recorder_mock)
    print("\n=== reset_ledger:", resp)
    assert resp["reset_ledger"][entry.entry_id]["statistics_cleared"] is True
    assert _s(hass, "sensor.tai_dian_ahu_current_period_usage") in (None, "unknown")

    after = (await _stat_rows(hass, kwh_id, date(2026, 9, 1), date(2026, 10, 14))).get(kwh_id) or []
    assert after == []
