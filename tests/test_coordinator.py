"""Coordinator logic tests, run against a minimal stub of Home Assistant.

What these DO cover: the period-window arithmetic, the ``config_issues`` /
``config_warning`` detection, the "don't cascade into empty sensors" behaviour,
and the guard that stops 「結算本期」 from walking 抄見日 into the future.  These are
the exact mechanisms behind the "almost every value is empty" regression.

What these do NOT cover: real Home Assistant API compatibility (no config-entry
plumbing, no entity registry, no live database).  The recorder *call shape* IS
covered, against a stub mirroring HA 2026.10.1's module-level
``statistics_during_period``, and the arguments it receives are asserted
directly -- that is how an ``end_time + 1 day`` off-by-one was caught.

Run:  python3 -I tests/test_coordinator.py
"""

import asyncio
import importlib.util
import logging
import sys
import types
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "taipower_bill"
_TZ = timezone(timedelta(hours=8))
NOW = [datetime(2026, 10, 14, 12, 0, tzinfo=_TZ)]  # the day the bug was reported

failures = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got {got!r} want {want!r}")
    if not ok:
        failures.append(label)


def ok(label, condition, detail=""):
    print(f"{'PASS' if condition else 'FAIL'}  {label}{(' — ' + str(detail)) if detail else ''}")
    if not condition:
        failures.append(label)


# --------------------------------------------------------------------------
# 1. minimal Home Assistant stub
# --------------------------------------------------------------------------
def _module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__path__ = []
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


def _dt_now():
    return NOW[0]


def _start_of_local_day(value):
    return datetime(value.year, value.month, value.day, tzinfo=_TZ)


ha = _module("homeassistant")
util = _module("homeassistant.util")
dt_mod = _module(
    "homeassistant.util.dt",
    now=_dt_now,
    UTC=timezone.utc,
    start_of_local_day=_start_of_local_day,
    as_local=lambda dt: dt.astimezone(_TZ),
    utc_from_timestamp=lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc),
)


class HomeAssistantError(Exception):
    pass


class ConfigEntryError(Exception):
    pass


class DataUpdateCoordinator:
    def __init__(self, hass, logger, name=None, update_interval=None):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data = None

    async def async_request_refresh(self):
        return None


class Store:
    def __init__(self, hass, version, key):
        self.hass, self.version, self.key = hass, version, key

    async def async_load(self):
        return None

    async def async_save(self, data):
        self.saved = data


class ConfigEntry:
    """Stand-in; the coordinator only reads data/options/entry_id."""


@dataclass(frozen=True)
class SensorEntityDescription:
    """Mirrors the real (frozen) dataclass, so TaipowerSensorDescription's
    generated __init__ inherits the same field set."""

    key: str = ""
    translation_key: str | None = None
    native_unit_of_measurement: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    suggested_display_precision: int | None = None
    icon: str | None = None


components = _module("homeassistant.components")
_module(
    "homeassistant.components.sensor",
    SensorEntityDescription=SensorEntityDescription,
    SensorDeviceClass=types.SimpleNamespace(ENERGY="energy", MONETARY="monetary", DURATION="duration", DATE="date"),
    SensorStateClass=types.SimpleNamespace(MEASUREMENT="measurement", TOTAL="total"),
)
_module("homeassistant.config_entries", ConfigEntry=ConfigEntry, ConfigFlow=object, OptionsFlow=object)
_module(
    "homeassistant.const",
    STATE_UNAVAILABLE="unavailable",
    STATE_UNKNOWN="unknown",
    UnitOfEnergy=types.SimpleNamespace(KILO_WATT_HOUR="kWh"),
    UnitOfTime=types.SimpleNamespace(DAYS="d"),
)
_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError, ConfigEntryError=ConfigEntryError)
helpers = _module("homeassistant.helpers")
_module("homeassistant.helpers.storage", Store=Store)
_module("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=DataUpdateCoordinator)

# -- recorder stub: mirrors HA 2026.10.1, where statistics_during_period() is a
# -- module-level function taking hass first (NOT a Recorder method).
RECORDER_CALLS = []
RECORDER_RESULT = {}
RECORDER_ERROR = [None]


def _statistics_during_period(hass, start_time, end_time, statistic_ids, period, units, types):
    RECORDER_CALLS.append(
        {
            "hass": hass,
            "start": start_time,
            "end": end_time,
            "ids": statistic_ids,
            "period": period,
            "units": units,
            "types": types,
        }
    )
    if RECORDER_ERROR[0] is not None:
        raise RECORDER_ERROR[0]
    return RECORDER_RESULT


class FakeRecorderInstance:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


_module("homeassistant.components.recorder", get_instance=lambda hass: FakeRecorderInstance())
_module(
    "homeassistant.components.recorder.statistics",
    statistics_during_period=_statistics_during_period,
)

ha.util = util
ha.components = components
ha.helpers = helpers
util.dt = dt_mod

# --------------------------------------------------------------------------
# 2. load the integration modules by path (the real __init__ imports HA)
# --------------------------------------------------------------------------
pkg = types.ModuleType("taipower_bill")
pkg.__path__ = [str(PKG)]
sys.modules["taipower_bill"] = pkg


def _load(name):
    spec = importlib.util.spec_from_file_location(f"taipower_bill.{name}", PKG / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


for _name in ("tariffs", "billing", "validators", "const", "coordinator"):
    _load(_name)

coordinator_mod = sys.modules["taipower_bill.coordinator"]
TaipowerDataUpdateCoordinator = coordinator_mod.TaipowerDataUpdateCoordinator

logging.basicConfig(level=logging.CRITICAL)  # the ERROR-level config logs are expected


# --------------------------------------------------------------------------
# 3. fake hass / entry
# --------------------------------------------------------------------------
class FakeState:
    def __init__(self, state):
        self.state = state


class FakeStates:
    def __init__(self, values=None):
        self._values = values or {}

    def get(self, entity_id):
        value = self._values.get(entity_id)
        if value is None:
            return None
        return FakeState(value) if not hasattr(value, "state") else value


class FakeConfigEntries:
    def async_update_entry(self, entry, **kwargs):
        if "data" in kwargs:
            entry.data = kwargs["data"]


class FakeHass:
    def __init__(self, states=None):
        self.states = FakeStates(states)
        self.config_entries = FakeConfigEntries()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FakeEntry:
    def __init__(self, data):
        self.entry_id = "test_entry"
        self.data = dict(data)
        self.options = {}


def make_coordinator(config, states=None):
    entry = FakeEntry(config)
    return TaipowerDataUpdateCoordinator(FakeHass(states), entry)


BASE = {
    "label": "B戶",
    "source_entities": ["sensor.house_b_energy_this_month"],
    "source_mode": "manual",
    "cycle_days": 60,
}


async def main():
    # -- A. the broken entry: 抄見日 2028-04-29 --------------------------------
    print("\nA. broken config (上期抄見日 in the future -- the state the entry was found in)")
    a = make_coordinator({**BASE, "last_read_date": "2028-04-29"})
    check("period_start (was on the device page)", a.period_start.isoformat(), "2028-04-30")
    check("period_end (was on the device page)", a.period_end.isoformat(), "2028-06-28")
    check("days_elapsed", a.days_elapsed, 0)
    check(
        "days the old code reported as days_until_next_read",
        (a.period_end - a.today).days,
        623,
    )
    ok("period_has_started is False", a.period_has_started is False)
    check("config_issues", a.config_issues, ["last_read_in_future"])
    ok(
        "config_warning explains it",
        "本期尚未開始" in (a._config_warning() or "") and "結算本期" in (a._config_warning() or ""),
    )

    data = await a._async_update_data()
    check("usage_source", data["usage_source"], "period-not-started")
    check("kwh", data["kwh"], None)
    check("cost", data["cost"], None)
    check("days_remaining", data["days_remaining"], None)
    check("bill object", data["bill"], None)
    check("baseline untouched", a.ledger.data["baseline_kwh"], None)

    # The button must refuse instead of advancing 抄見日 another 60 days.
    try:
        await a.async_close_period()
        ok("close_period refused", False, "it did NOT raise")
    except HomeAssistantError as err:
        ok("close_period refused with a readable message", "尚未開始" in str(err), str(err)[:70])
    check("last_read_date unchanged after refused press", a.last_read_date.isoformat(), "2028-04-29")

    # A future 抄見日 cannot be saved again through the service either.
    try:
        await a.async_set_last_read_date(date(2029, 1, 1))
        ok("service rejects a future date", False, "it did NOT raise")
    except HomeAssistantError as err:
        ok("service rejects a future date", "不能是未來日期" in str(err))

    # The repair path: point it at the real bill date, wiping the junk history.
    a.ledger.data["periods"] = [{"period_start": "2026-09-08"}]
    result = await a.async_set_last_read_date(date(2026, 9, 7), reset_history=True)
    check("repair -> period_start", result["period_start"], "2026-09-08")
    check("repair -> history_cleared", result["history_cleared"], 1)
    check("repair -> config_issues empty", a.config_issues, [])
    data = await a._async_update_data()
    check("after repair, usage_source", data["usage_source"], "manual")
    check("after repair, days_elapsed", data["days_elapsed"], 37)
    check("after repair, days_remaining", data["days_remaining"], 23)

    # -- B. a healthy mid-period install -------------------------------------
    print("\nB. healthy config mid-period (上期抄見日 2026-09-07, 350 kWh keyed in)")
    b = make_coordinator({**BASE, "last_read_date": "2026-09-07"})
    b.manual_kwh = 350.0
    data = await b._async_update_data()
    check("kwh", data["kwh"], 350.0)
    check("days_elapsed", data["days_elapsed"], 37)
    check("days_remaining", data["days_remaining"], 23)
    check("bill window days", data["bill"].days, 37)
    ok("cost is positive", data["cost"] > 0, data["cost"])
    ok("projected cost > current cost", data["projected_cost"] > data["cost"], data["projected_cost"])

    # Repeated presses in one day must not walk the date forward.
    try:
        await b.async_close_period()
        ok("unfinished period refuses a press", False, "it did NOT raise")
    except HomeAssistantError as err:
        ok("unfinished period refuses a press", "還沒到期" in str(err), str(err)[:60])
    check("last_read_date unchanged after refused press", b.last_read_date.isoformat(), "2026-09-07")

    # Ten refused presses later the date is still where it was.
    for _ in range(10):
        try:
            await b.async_close_period()
        except HomeAssistantError:
            pass
    check("after 10 refused presses, 抄見日 unchanged", b.last_read_date.isoformat(), "2026-09-07")

    # An explicit force still works (closing early on purpose).
    record = await b.async_close_period(actual_kwh=350.0, actual_bill=700.0, force=True)
    check("forced close recorded kwh", record["kwh"], 350.0)
    check("forced close variance", record["variance"], round(record["estimated_total"] - 700.0, 2))
    check("forced close advanced 抄見日", b.last_read_date.isoformat(), "2026-11-06")
    check("history length", len(b.ledger.periods), 1)

    # -- C. statistics mode: the recorder call shape --------------------------
    print("\nC. statistics mode (recorder daily statistics)")

    def rows(changes, first_stamp, unit_ms=True):
        step = 86400 * (1000 if unit_ms else 1)
        out = []
        for i, change in enumerate(changes):
            stamp = first_stamp + i * step
            out.append({"start": stamp, "end": stamp, "change": change, "sum": None})
        return out

    RECORDER_CALLS.clear()
    RECORDER_ERROR[0] = None
    RECORDER_RESULT.clear()
    # source 1 hands back millisecond stamps, source 2 second stamps -- the
    # helper has to cope with both, because HA's websocket layer rewrites the
    # field to ms while the in-process function returns seconds.
    RECORDER_RESULT["sensor.house_a_energy_this_month"] = rows(
        [10.0, 20.0, 30.0], 1788796800000, unit_ms=True
    )
    RECORDER_RESULT["sensor.house_b_energy_this_month"] = rows(
        [1.5, 2.5], 1791820800, unit_ms=False
    )

    c = make_coordinator(
        {
            "label": "A戶",
            "source_entities": [
                "sensor.house_a_energy_this_month",
                "sensor.house_b_energy_this_month",
            ],
            "source_mode": "statistics",
            "last_read_date": "2026-09-07",
            "cycle_days": 60,
        }
    )
    data = await c._async_update_data()
    check("kwh = sum of per-row change across both sources", data["kwh"], 64.0)
    check("usage_source", data["usage_source"], "statistics")
    check("stats_error", data["stats_error"], None)
    check("stats_coverage_start", data["stats_coverage_start"], date(2026, 9, 8))
    check("stats_last_day (ms stamps parsed)", data["stats_last_day"], date(2026, 10, 14))
    check("cost = 64 kWh at the 1.78 tier-1 rate", round(data["cost"], 2), 113.92)

    call = RECORDER_CALLS[-1]
    check(
        "one batched query for every source",
        call["ids"],
        {"sensor.house_a_energy_this_month", "sensor.house_b_energy_this_month"},
    )
    check("period", call["period"], "day")
    check("types", call["types"], {"change", "sum"})
    check("units left to HA", call["units"], None)
    check("start_time", call["start"], datetime(2026, 9, 8, tzinfo=_TZ))
    # HA rounds end_time up to the end of its own day, so the window end must be
    # passed as-is; +1 day would silently widen the window by a day.
    check("end_time is the window end, not end + 1 day", call["end"], datetime(2026, 10, 14, tzinfo=_TZ))

    RECORDER_RESULT.clear()
    RECORDER_RESULT["sensor.house_a_energy_this_month"] = [
        {"start": 1788796800000, "end": 1788883200000, "change": None, "sum": 100.0},
        {"start": 1788883200000, "end": 1788969600000, "change": None, "sum": 130.0},
    ]
    c.source_entities = ["sensor.house_a_energy_this_month"]
    data = await c._async_update_data()
    check("falls back to sum[-1] - sum[0] when change is missing", data["kwh"], 30.0)

    # -- C2. a statistics failure must be visible, never silent --------------
    print("\nC2. statistics failure is surfaced, not swallowed")
    RECORDER_ERROR[0] = RuntimeError("cannot connect to database")
    c2 = make_coordinator(
        {
            "label": "A戶",
            "source_entities": ["sensor.house_a_energy_this_month"],
            "source_mode": "statistics",
            "last_read_date": "2026-09-07",
            "cycle_days": 60,
        },
        states={"sensor.house_a_energy_this_month": 321.5},
    )
    data = await c2._async_update_data()
    ok("stats_error names the exception", "RuntimeError" in (data["stats_error"] or ""), data["stats_error"])
    check("falls back to the live-delta path", data["usage_source"], "live-delta")
    check("first live-delta poll reports 0 (baseline just captured)", data["kwh"], 0.0)
    check("baseline stored", c2.ledger.data["baseline_kwh"], 321.5)

    c2.hass.states._values["sensor.house_a_energy_this_month"] = 330.0
    data = await c2._async_update_data()
    check("second poll reports the delta", data["kwh"], 8.5)

    RECORDER_ERROR[0] = None
    RECORDER_RESULT.clear()
    RECORDER_RESULT["sensor.house_a_energy_this_month"] = rows([5.0], 1791820800, unit_ms=False)
    data = await c2._async_update_data()
    check(
        "recovers to statistics once the recorder answers",
        (data["stats_error"], data["usage_source"]),
        (None, "statistics"),
    )
    RECORDER_RESULT.clear()
    data = await c2._async_update_data()
    ok("an empty result is reported as such", "沒有回傳任何資料列" in (data["stats_error"] or ""), data["stats_error"])

    # -- D. sensor state classes must be legal for their device class ---------
    print("\nD. sensor (device_class, state_class) pairs")
    # Copied from homeassistant/components/sensor/const.py, HA 2026.10.1
    # (DEVICE_CLASS_STATE_CLASSES).  A pair outside the set already logs
    # "is using state class X which is impossible considering device class Y"
    # and is slated to become a hard error, so it is worth asserting.
    _allowed = {
        "energy": {"total", "total_increasing"},
        "monetary": {"total"},
        "duration": {"total", "total_increasing", "measurement", "measurement_angle"},
        "date": set(),
    }
    descriptions = sys.modules["taipower_bill.const"].SENSOR_DESCRIPTIONS
    check("sensor count", len(descriptions), 11)
    for desc in descriptions:
        legal = (
            desc.state_class is None
            or desc.device_class not in _allowed
            or desc.state_class in _allowed[desc.device_class]
        )
        ok(
            f"{desc.key}: device_class={desc.device_class!r} state_class={desc.state_class!r}",
            legal,
        )
    check(
        "only the two accumulating sensors declare total",
        sorted(d.key for d in descriptions if d.state_class == "total"),
        ["period_cost", "period_kwh"],
    )
    check(
        "TWD/kWh sensors carry no monetary device class",
        {d.key: d.device_class for d in descriptions if d.key.endswith("price_per_kwh")},
        {"avg_price_per_kwh": None, "marginal_price_per_kwh": None},
    )

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("ALL COORDINATOR TESTS PASSED")


asyncio.run(main())
