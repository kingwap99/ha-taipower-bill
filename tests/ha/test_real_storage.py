"""The v0.1 -> v0.2 store migration, on a real Home Assistant.

This is the regression that made v0.2.0 unusable, so it is pinned against the
real ``Store``: ``Store.__init__`` no longer accepts a migration keyword, so
constructing the coordinator raised ``TypeError``, ``async_setup_entry`` failed,
and the entry ended up NOT_LOADED with **zero entities**.  A stub ``Store`` that
accepted the keyword hid it from the unit suite for a whole release.

``hass_storage`` is the plugin's in-memory storage mock (the ``hass`` fixture
depends on it), and it deliberately routes reads through the real
``Store._async_load`` so that migration still happens.
"""
from __future__ import annotations

from datetime import UTC, datetime

from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

DOMAIN = "taipower_bill"
TODAY = datetime(2026, 10, 14, 20, 0, tzinfo=UTC)  # 13:00 US/Pacific (the harness tz)
STORAGE_KEY = "taipower_bill_ledger"
OPTIONS = {
    "label": "A戶",
    "source_entities": ["sensor.house_a_energy_this_month"],
    "source_mode": "manual",
    "cycle_days": 60,
    # v0.1.x kept the anchor in the config entry, not in the ledger
    "last_read_date": "2026-07-10",
}

# A v0.1.x payload: one row per 結算本期 press.
V1_PERIOD = {
    "period_start": "2026-07-11",
    "period_end": "2026-09-07",
    "days": 59,
    "kwh": 700.0,
    "actual_total": 1750.0,
    "energy_cost": 1700.0,
    "surcharge": 0.0,
    "fixed_fees": 0.0,
    "tax": 0.0,
    "estimated_total": 1755.0,
    "avg_price_per_kwh": 2.5,
    "seasons": ["summer"],
    "source": "statistics",
    "forced": False,
}


def _s(hass: HomeAssistant, eid: str):
    st = hass.states.get(eid)
    return None if st is None else st.state


def _attrs(hass: HomeAssistant, eid: str) -> dict:
    return dict(hass.states.get(eid).attributes)


@freeze_time(TODAY)
async def test_v1_store_is_migrated_and_history_survives(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, title="A戶", options=dict(OPTIONS), unique_id="taipower_bill.a_house"
    )
    entry.add_to_hass(hass)

    key = f"{STORAGE_KEY}.{entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"periods": [V1_PERIOD], "baseline_kwh": None, "baseline_date": None},
    }

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state.value == "loaded"

    a = _attrs(hass, "sensor.tai_dian_ahu_current_period_usage")
    print("\n=== migrated readings:", a["readings"])
    # A v0.1 closed period's ``period_end`` *is* a read date, and the v0.1 anchor
    # comes across from the config entry; both are sorted oldest first.
    assert [r["date"] for r in a["readings"]] == ["2026-07-10", "2026-09-07"]
    # the amounts recorded back then are preserved rather than recomputed
    assert _s(hass, "sensor.tai_dian_ahu_previous_period_energy") == "700.0"
    assert float(_s(hass, "sensor.tai_dian_ahu_previous_period_cost")) == 1750.0
    assert float(_s(hass, "sensor.tai_dian_ahu_estimate_vs_actual_variance")) == 5.0
    # and the current window is derived from the *latest* read date
    assert a["last_read_date"] == "2026-09-07"
    assert a["period_start"] == "2026-09-08"
    assert a["period_end"] == "2026-11-06"

    # the store is written back at v2, so the migration runs exactly once
    stored = hass_storage[key]
    print("=== stored after migration:", stored["version"], sorted(stored["data"]))
    assert stored["version"] == 2
    assert "periods" not in stored["data"]
    assert [r["date"] for r in stored["data"]["readings"]] == ["2026-07-10", "2026-09-07"]

    # a reload re-reads the v2 payload without disturbing the history
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    a2 = _attrs(hass, "sensor.tai_dian_ahu_current_period_usage")
    assert [r["date"] for r in a2["readings"]] == ["2026-07-10", "2026-09-07"]
    assert _s(hass, "sensor.tai_dian_ahu_previous_period_energy") == "700.0"


@freeze_time(TODAY)
async def test_a_newer_stored_payload_is_never_clobbered(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """Downgrading must not silently wipe a ledger written by a newer version."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="A戶", options=dict(OPTIONS), unique_id="taipower_bill.a_house"
    )
    entry.add_to_hass(hass)
    key = f"{STORAGE_KEY}.{entry.entry_id}"
    hass_storage[key] = {
        "version": 99,
        "minor_version": 1,
        "key": key,
        "data": {"readings": [{"date": "2026-09-07", "note": "from the future"}]},
    }

    try:
        loaded = await hass.config_entries.async_setup(entry.entry_id)
    except Exception as err:  # noqa: BLE001 - the point is that it is loud
        loaded = False
        print("\n=== setup raised:", type(err).__name__)
    await hass.async_block_till_done()
    print("=== loaded:", loaded, "state:", entry.state)

    assert loaded is False
    assert entry.state.value != "loaded"
    # the only thing that really matters: the newer payload is still intact
    assert hass_storage[key]["version"] == 99
    assert hass_storage[key]["data"]["readings"][0]["note"] == "from the future"
