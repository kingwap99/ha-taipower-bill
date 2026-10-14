"""Coordinator + sensor logic tests, run against a minimal stub of Home Assistant.

v0.2.0 changed the model, so this suite is written around it: the source of
truth is a **list of actual 抄見日**, a period is (previous read date + 1 day) →
(this read date), closing is propose → confirm, and every closed period is
re-derivable and published as an external long-term statistic.

What these tests DO cover:

* the period arithmetic derived from the read-date list, including a *future*
  下次抄表日 replacing the ``cycle_days`` projection;
* that a proposed 結算 writes nothing until it is confirmed, and that it expires;
* the rescue paths (undo / rewrite / recompute) and the refusals that stop a
  mis-press from corrupting the ledger;
* the recorder call shape (module-level ``statistics_during_period``, ``end``
  must NOT be ``end + 1 day``, per-row ``change`` summing) and the external
  statistics payload (top-of-hour tz-aware rows, cumulative sums, metadata
  fields HA 2026.10 requires);
* sensor legality: every (device_class, state_class) pair, ``last_reset`` only
  on the ``total`` sensors, and the attribute contract the Lovelace card reads.

What these do NOT cover: real Home Assistant plumbing.  ``tests/ha/`` does,
against a real Home Assistant 2026.10.1 (see ``requirements_test_ha.txt``):
setup through a config entry, the entity registry, a live recorder database,
the nine services, and the v1 -> v2 store migration.

Every stub in here is a place where a bug can hide, so the stubs mirror the
*real* signatures and only the real signatures.  v0.2.0 shipped with a fake
``Store`` whose constructor accepted a migration keyword that core had removed
(and one stub `Store` too many -- the recorder's ``async_clear_statistics`` was
stubbed at module level, where it no longer exists); the real integration died
on setup and neither this suite nor CI could see it.  If a stub is more
permissive than Home Assistant, it is a bug in the stub.

Run:  python3 -I tests/test_coordinator.py        (needs Python 3.11+)
"""

import asyncio
import copy
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

# The coordinator uses ``asyncio.timeout``, which is 3.11+.  Home Assistant
# 2026.10 needs 3.13 anyway, so this is only a guard against a confusing
# AttributeError on an old interpreter.
if sys.version_info < (3, 11):  # pragma: no cover
    sys.exit(f"test_coordinator.py needs Python 3.11+ (running {sys.version.split()[0]})")

failures = []
passes = [0]


def check(label, got, want):
    ok_ = got == want
    passes[0] += 1
    print(f"{'PASS' if ok_ else 'FAIL'}  {label}: got {got!r} want {want!r}")
    if not ok_:
        failures.append(label)


def ok(label, condition, detail=""):
    passes[0] += 1
    print(f"{'PASS' if condition else 'FAIL'}  {label}{(' — ' + str(detail)) if detail else ''}")
    if not condition:
        failures.append(label)


async def araises(label, coro, needle):
    """Assert that awaiting ``coro`` raises HomeAssistantError containing needle.

    Async (not a ``asyncio.run`` wrapper) because these are called from inside a
    running loop.
    """
    try:
        await coro
    except HomeAssistantError as err:
        ok(label, needle in str(err), str(err)[:130])
    except Exception as err:  # noqa: BLE001
        ok(label, False, f"wrong exception {type(err).__name__}: {err}")
    else:
        ok(label, False, "it did NOT raise")


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


def _parse_datetime(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


ha = _module("homeassistant")
util = _module("homeassistant.util")
dt_mod = _module(
    "homeassistant.util.dt",
    now=_dt_now,
    UTC=timezone.utc,
    start_of_local_day=_start_of_local_day,
    parse_datetime=_parse_datetime,
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
        self.last_update_success = True

    async def async_request_refresh(self):
        return None


STORE_DATA = {}
STORE_SAVES = []


class Store:
    """In-memory stand-in for ``homeassistant.helpers.storage.Store``.

    The signature is deliberately the *real* one as of HA 2026.10::

        Store(hass, version, key, *, minor_version=1, atomic_writes=False,
              encoder=None, decoder=None)

    There is **no** migration keyword -- the old ``migrate_func`` argument was
    removed from core, and ``async_migrate_func`` never existed.  v0.2.0 passed
    ``async_migrate_func=`` anyway, so ``TypeError`` was raised inside the
    coordinator's constructor, ``async_setup_entry`` died and the integration
    produced no entities at all on a real instance -- while this stub stayed
    green precisely because its signature accepted the keyword.  Mirroring the
    real signature is the entire point of having the stub.

    Migration therefore has one legal route, reproduced faithfully here: a
    subclass overrides ``_async_migrate_func``, and ``async_load`` calls it when
    the stored version does not match the store's (core's
    ``Store._async_load_data``).  A base ``Store`` raises ``NotImplementedError``.
    """

    def __init__(
        self,
        hass,
        version,
        key,
        *,
        minor_version=1,
        atomic_writes=False,
        encoder=None,
        decoder=None,
    ):
        self.hass, self.version, self.key = hass, version, key
        self.minor_version = minor_version
        self.saved = None

    async def _async_migrate_func(self, old_major, old_minor, old_data):
        """A plain Store has no migration hook; core raises."""
        raise NotImplementedError

    async def async_load(self):
        envelope = STORE_DATA.get(self.key)
        if envelope is None:
            return None
        # The real Store hands back a *fresh* object decoded from JSON, so two
        # coordinators sharing a store key never alias one readings list.  Copy
        # here too, otherwise the stub would hide exactly the bugs it tests for.
        envelope = copy.deepcopy(envelope)
        data = envelope.get("data")
        if (
            envelope.get("version") == self.version
            and envelope.get("minor_version") == self.minor_version
        ):
            return data
        return await self._async_migrate_func(
            envelope.get("version", 1), envelope.get("minor_version", 1), data
        )

    async def async_save(self, data):
        self.saved = data
        STORE_DATA[self.key] = {
            "version": self.version,
            "minor_version": self.minor_version,
            "key": self.key,
            "data": copy.deepcopy(data),
        }
        STORE_SAVES.append(self.key)


class ConfigEntry:
    """Stand-in; the coordinator reads data/options/entry_id/unique_id."""


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


class SensorEntity:
    """Stub whose ``state_attributes`` reproduces HA's ``last_reset`` rule."""

    _attr_has_entity_name = False

    @property
    def state_attributes(self):
        reset = getattr(self, "last_reset", None)
        if reset is not None and self.entity_description.state_class != "total":
            raise AttributeError(
                "Entity has a last_reset but is not a total sensor"
            )
        return self.extra_state_attributes


components = _module("homeassistant.components")
_module(
    "homeassistant.components.sensor",
    SensorEntityDescription=SensorEntityDescription,
    SensorEntity=SensorEntity,
    SensorDeviceClass=types.SimpleNamespace(
        ENERGY="energy", MONETARY="monetary", DURATION="duration", DATE="date"
    ),
    SensorStateClass=types.SimpleNamespace(MEASUREMENT="measurement", TOTAL="total"),
)
_module(
    "homeassistant.config_entries",
    ConfigEntry=ConfigEntry,
    ConfigFlow=object,
    OptionsFlow=object,
)
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


class CoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.hass = coordinator.hass

    @property
    def available(self):
        return bool(self.coordinator.last_update_success)


_module(
    "homeassistant.helpers.update_coordinator",
    DataUpdateCoordinator=DataUpdateCoordinator,
    CoordinatorEntity=CoordinatorEntity,
)
_module(
    "homeassistant.helpers.device_registry",
    DeviceEntryType=types.SimpleNamespace(SERVICE="service"),
    DeviceInfo=dict,
)
_module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)

# -- recorder stub: mirrors HA 2026.10.1, where statistics_during_period() is a
# -- module-level function taking hass first (NOT a Recorder method).
RECORDER_CALLS = []
RECORDER_RESULT = {}
RECORDER_ERROR = [None]
EXTERNAL_STATS = []
CLEARED_STATS = []
EXTERNAL_STATS_ERROR = [None]


def _row_local_day(raw):
    """Local date of a stub row's ``start`` (a UTC-midnight timestamp)."""
    stamp = datetime.fromtimestamp(float(raw), tz=timezone.utc)
    return stamp.astimezone(_TZ).date()


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

    # Mirror the real recorder: rows are clipped to the requested window, and
    # ``end_time``'s own day is *included* (HA rounds end_time up to the end of
    # its day).  This is what makes the 「end 不該 +1 天」 test meaningful.
    out = {}
    for entity_id, rows in RECORDER_RESULT.items():
        if entity_id not in statistic_ids:
            continue
        kept = [
            row
            for row in rows
            if start_time.date() <= _row_local_day(row.get("start")) <= end_time.date()
        ]
        if kept:
            out[entity_id] = kept
    return out


def _async_add_external_statistics(hass, metadata, statistics):
    if EXTERNAL_STATS_ERROR[0] is not None:
        raise EXTERNAL_STATS_ERROR[0]
    EXTERNAL_STATS.append({"hass": hass, "metadata": metadata, "statistics": list(statistics)})


class StatisticMeanType:
    NONE = 0
    ARITHMETIC = 1


class FakeRecorderInstance:
    """Mirrors the parts of the real ``Recorder`` the coordinator uses.

    ``async_clear_statistics`` lives on the **instance** -- HA 2026.10's
    signature is ``Recorder.async_clear_statistics(statistic_ids, *,
    on_done=None)`` and it is a plain ``@callback`` (no ``await``), reached
    through ``get_instance(hass)``.  ``recorder/statistics.py`` only has the
    *blocking* ``clear_statistics``, so there is deliberately no module-level
    ``async_clear_statistics`` here either: an importable module-level helper
    is exactly the shape that let the v0.2.0 ImportError go unnoticed.
    """

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def async_clear_statistics(self, statistic_ids, *, on_done=None):
        CLEARED_STATS.append(list(statistic_ids))
        if on_done is not None:
            on_done()


_module("homeassistant.components.recorder", get_instance=lambda hass: FakeRecorderInstance())
_module(
    "homeassistant.components.recorder.models",
    StatisticMeanType=StatisticMeanType,
)
_module(
    "homeassistant.components.recorder.statistics",
    statistics_during_period=_statistics_during_period,
    async_add_external_statistics=_async_add_external_statistics,
    StatisticMeanType=StatisticMeanType,
)

ha.util = util
ha.components = components
ha.helpers = helpers
util.dt = dt_mod

# --------------------------------------------------------------------------
# 2. load the integration modules by path (the real package imports HA)
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


for _name in ("tariffs", "billing", "validators", "const", "coordinator", "sensor"):
    _load(_name)

coordinator_mod = sys.modules["taipower_bill.coordinator"]
const_mod = sys.modules["taipower_bill.const"]
sensor_mod = sys.modules["taipower_bill.sensor"]
validators_mod = sys.modules["taipower_bill.validators"]
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


class FakeServices:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data=None, blocking=False, **kwargs):
        self.calls.append({"domain": domain, "service": service, "data": dict(data or {})})

    def notifications(self, service):
        return [
            c["data"]
            for c in self.calls
            if c["domain"] == "persistent_notification" and c["service"] == service
        ]


class FakeConfigEntries:
    def async_update_entry(self, entry, **kwargs):
        if "data" in kwargs:
            entry.data = kwargs["data"]


class FakeHass:
    def __init__(self, states=None):
        self.states = FakeStates(states)
        self.config_entries = FakeConfigEntries()
        self.services = FakeServices()
        # The real HomeAssistant sets ``self.loop`` in ``__init__``
        # (core.py: ``self.loop = asyncio.get_running_loop()``) and the
        # statistics-clear path uses it, so the stub needs one too.
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - constructed outside a loop
            self.loop = asyncio.new_event_loop()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FakeEntry:
    def __init__(self, data, unique_id="taipower_bill.b_house"):
        self.entry_id = "test_entry"
        self.unique_id = unique_id
        self.data = dict(data)
        self.options = {}


def make_coordinator(config, states=None, unique_id="taipower_bill.b_house"):
    entry = FakeEntry(config, unique_id=unique_id)
    return TaipowerDataUpdateCoordinator(FakeHass(states), entry)


BASE = {
    "label": "B戶",
    "source_entities": ["sensor.house_b_energy_this_month"],
    "source_mode": "manual",
    "cycle_days": 60,
}


async def refresh(c):
    """Reproduce what DataUpdateCoordinator does with _async_update_data()."""
    c.data = await c._async_update_data()
    return c.data


def readings(c):
    return [r.get("date") for r in c.readings]


def ts(y, m, d):
    """A statistics row timestamp: seconds since the epoch, UTC."""
    return datetime(y, m, d, tzinfo=timezone.utc).timestamp()


def daily_rows(*values, first=(2026, 9, 8)):
    """Build recorder rows with per-row ``change`` starting at ``first``."""
    day = date(*first)
    rows = []
    total = 0.0
    for value in values:
        total += value
        rows.append(
            {
                "start": ts(day.year, day.month, day.day),
                "change": value,
                "sum": round(total, 6),
            }
        )
        day += timedelta(days=1)
    return rows


def reset_recorder():
    RECORDER_CALLS.clear()
    EXTERNAL_STATS.clear()
    CLEARED_STATS.clear()
    EXTERNAL_STATS_ERROR[0] = None
    RECORDER_ERROR[0] = None
    RECORDER_RESULT.clear()
    STORE_SAVES.clear()


# ==========================================================================
# 4. the tests
# ==========================================================================
NOW_DEFAULT = datetime(2026, 10, 14, 12, 0, tzinfo=_TZ)


def _reset_now():
    NOW[0] = NOW_DEFAULT


async def test_read_date_model():
    """Everything is derived from the read-date list, nothing is stored twice."""
    reset_recorder()
    STORE_DATA.clear()
    c = make_coordinator({**BASE, "last_read_date": "2026-09-07"})
    await c.async_load_ledger()

    check("anchor becomes a reading", readings(c), ["2026-09-07"])
    check("period_start = 抄見日 + 1", c.period_start, date(2026, 9, 8))
    check("period_end projected from cycle_days", c.period_end, date(2026, 11, 6))
    check("next_read_date mirrors period_end", c.next_read_date, date(2026, 11, 6))
    check("last_read_date", c.last_read_date, date(2026, 9, 7))
    check("days_elapsed", c.days_elapsed, 37)
    check("days_remaining", c.days_remaining, 23)
    check("period has started", c.period_has_started, True)
    check("period has not ended", c.period_has_ended, False)
    check("healthy config", c.config_issues, [])
    check("suggested date is today while the window is open", c.suggested_read_date, date(2026, 10, 14))
    check("no note before the read day", c.period_note(), None)

    # A future 下次抄表日 replaces the projection -- that is the point of
    # supporting a date the user has not reached yet.
    await c.async_add_reading(date(2026, 11, 20))
    check("future read date wins over the projection", c.period_end, date(2026, 11, 20))
    check("last_read_date unchanged by a future date", c.last_read_date, date(2026, 9, 7))

    c.manual_kwh = 350.0
    data = await refresh(c)
    check("days_in_period", data["days_in_period"], 74)
    check("days_elapsed still capped at today", data["days_elapsed"], 37)
    check("kwh in manual mode", data["kwh"], 350.0)
    check("usage_source", data["usage_source"], "manual")
    check("readings list is brief rows", sorted(data["readings"][0]), ["bill", "date", "forced", "kwh", "note"])
    check("no history yet", data["history_len"], 0)
    windows = c.period_windows()
    check("one window for two read dates", len(windows), 1)
    check("window bounds", (windows[0][0], windows[0][1]), (date(2026, 9, 8), date(2026, 11, 20)))
    check("no window is closed yet", c.closed_windows(), [])


async def test_on_the_read_day():
    """Pressing on (or after) the projected read day must not move the window."""
    reset_recorder()
    STORE_DATA.clear()
    NOW[0] = datetime(2026, 11, 6, 9, 0, tzinfo=_TZ)
    try:
        c = make_coordinator({**BASE, "last_read_date": "2026-09-07"})
        await c.async_load_ledger()
        c.manual_kwh = 500.0

        check("the projected day has arrived", c.period_has_ended, True)
        check("suggested date = the projected read day", c.suggested_read_date, date(2026, 11, 6))
        data = await refresh(c)
        check("days_elapsed covers the window", data["days_elapsed"], 60)
        check("days_remaining is 0", data["days_remaining"], 0)

        proposal = await c.async_close_period()
        check("accepted without force on the read day", proposal["pending"], True)
        check("proposed read date", proposal["read_date"], "2026-11-06")
        check("proposed period start", proposal["period_start"], "2026-09-08")

        result = await c.async_confirm_close()
        check("read date written", readings(c), ["2026-09-07", "2026-11-06"])
        check("new period starts the next day", c.period_start, date(2026, 11, 7))
        check("one closed window", result["readings"], ["2026-09-07", "2026-11-06"])
    finally:
        _reset_now()

    # The rescue mechanism behind the original complaint: three days late must
    # still record the day the meter was actually read.
    STORE_DATA.clear()
    NOW[0] = datetime(2026, 11, 9, 9, 0, tzinfo=_TZ)
    try:
        c2 = make_coordinator({**BASE, "last_read_date": "2026-09-07"})
        await c2.async_load_ledger()
        # Deliberately NOT polled: the 度數 number entity kicks off a refresh, but
        # a press can arrive first, and manual mode must still work.
        c2.manual_kwh = 500.0
        check("late: suggested date stays the projected day", c2.suggested_read_date, date(2026, 11, 6))
        proposal = await c2.async_close_period()
        check("late press proposes the original read date", proposal["read_date"], "2026-11-06")
        check("late press keeps the period start", proposal["period_start"], "2026-09-08")
    finally:
        _reset_now()


async def test_config_issues():
    reset_recorder()
    STORE_DATA.clear()

    empty = make_coordinator(BASE)
    await empty.async_load_ledger()
    check("no readings", empty.config_issues, ["no_readings"])
    check("no start date", empty.period_start, None)
    check("no days_elapsed", empty.days_elapsed, 0)
    check("no days_remaining", empty.days_remaining, None)
    check("no suggested date but still a date", empty.suggested_read_date, date(2026, 10, 14))
    data = await refresh(empty)
    check("source is not-started, not an exception", data["usage_source"], "period-not-started")
    check("no baseline written", empty.ledger.data.get("baseline_kwh"), None)
    ok("config_warning explains the empty list", "抄見日" in (data["config_warning"] or ""), data["config_warning"])
    ok("config_warning does not raise", data["config_issues"] == ["no_readings"])

    STORE_DATA.clear()
    future = make_coordinator(BASE)
    await future.async_load_ledger()
    await future.async_add_reading(date(2026, 12, 1))
    check("all-future list is its own issue", future.config_issues, ["no_current_reading"])
    check("no current reading", future.current_reading, None)
    check("not started", future.period_has_started, False)
    warn = (await refresh(future))["config_warning"] or ""
    ok("warning names the future date", "2026-12-01" in warn, warn[:80])

    STORE_DATA.clear()
    short = make_coordinator({**BASE, "cycle_days": 10})
    await short.async_load_ledger()
    check("short cycle flagged", short.config_issues, ["no_readings", "cycle_days_suspicious"])

    STORE_DATA.clear()
    nostats = make_coordinator(
        {**BASE, "source_mode": "statistics", "source_entities": [], "last_read_date": "2026-09-07"}
    )
    await nostats.async_load_ledger()
    check("statistics mode without sources flagged", nostats.config_issues, ["no_source_entities"])

    STORE_DATA.clear()
    stale = make_coordinator({**BASE, "last_read_date": "2026-09-07"})
    await stale.async_load_ledger()
    NOW[0] = datetime(2027, 3, 1, 12, 0, tzinfo=_TZ)
    try:
        check("stale read date flagged", stale.config_issues, ["last_read_stale"])
    finally:
        _reset_now()


async def test_close_propose_confirm():
    """A press must write nothing; only the confirmation touches the ledger."""
    reset_recorder()
    STORE_DATA.clear()
    c = make_coordinator({**BASE, "last_read_date": "2026-09-07"})
    await c.async_load_ledger()
    c.manual_kwh = 350.0
    await refresh(c)
    STORE_SAVES.clear()

    # 1. an early press is refused outright, and says why
    await araises("early close is refused", c.async_propose_close(), "提前結算")
    check("nothing pending after the refusal", c.pending, None)
    check("ledger untouched after the refusal", readings(c), ["2026-09-07"])

    # 2. force proposes, still writing nothing
    proposal = await c.async_propose_close(force=True)
    check("proposal pending", proposal["pending"], True)
    check("proposal read date = today", proposal["read_date"], "2026-10-14")
    check("proposal kwh", proposal["kwh"], 350.0)
    check("proposal force flag", proposal["force"], True)
    ok("proposal carries a bill estimate", (proposal["bill"] or {}).get("estimated_total", 0) > 0)
    check("proposal expires in 15 minutes", proposal["expires_at"], (NOW[0] + timedelta(minutes=15)).isoformat())
    check("ledger STILL untouched", readings(c), ["2026-09-07"])
    check("nothing saved yet", STORE_SAVES, [])

    created = c.hass.services.notifications("create")
    check("one persistent notification", len(created), 1)
    ok("notification titled for confirmation", "待確認結算" in created[0]["title"], created[0]["title"])
    ok("notification names the confirm action", "確認結算" in created[0]["message"])
    ok("notification id is entry scoped", created[0]["notification_id"].endswith("_pending"))

    payload = c.pending_payload()
    ok("pending_payload carries seconds_left", isinstance(payload.get("seconds_left"), int) and payload["seconds_left"] > 0, payload.get("seconds_left"))

    # 3. confirming writes, once
    result = await c.async_confirm_close()
    check("confirmed", result["confirmed"], True)
    check("reading appended", readings(c), ["2026-09-07", "2026-10-14"])
    # The 度數 has to be *stored* with the reading.  In manual mode the figure
    # only ever existed as ``manual_kwh``, and ``_write_reading`` zeroes that on
    # the way out -- so a confirmation that forgot to carry it over closed the
    # period blank: 上期/上上期 empty, and the external statistics series got no
    # rows at all.  (tests/ha catches this too; catching it here keeps the fast
    # suite honest as well.)
    check("the closed reading kept the manual 度數", c.ledger.get_reading(date(2026, 10, 14))["kwh"], 350.0)
    check("pending cleared after confirm", c.pending, None)
    check("new period opens the next day", c.period_start, date(2026, 10, 15))
    ok("ledger saved", bool(STORE_SAVES))
    ok("notification dismissed", bool(c.hass.services.notifications("dismiss")))
    check("the manual figure was consumed by the period", c.manual_kwh, 0.0)
    await araises("confirming twice is refused", c.async_confirm_close(), "沒有待確認")


async def test_cancel_and_expiry():
    reset_recorder()
    STORE_DATA.clear()
    c = make_coordinator({**BASE, "last_read_date": "2026-09-07"})
    await c.async_load_ledger()
    c.manual_kwh = 350.0
    await refresh(c)

    await c.async_propose_close(force=True)
    out = await c.async_cancel_close()
    check("cancel reports it had something to cancel", out["cancelled"], True)
    check("pending gone", c.pending, None)
    check("nothing written by cancel", readings(c), ["2026-09-07"])
    check("cancelling again is harmless", (await c.async_cancel_close())["cancelled"], False)

    # an expired proposal is invisible on the sensors and refused on confirm
    await c.async_propose_close(force=True)
    c.pending["expires_at"] = (NOW[0] - timedelta(minutes=1)).isoformat()
    check("expired proposals disappear from the sensors", c.pending_payload(), None)
    await araises("confirming an expired proposal is refused", c.async_confirm_close(), "逾時")

    await c.async_propose_close(force=True)
    c.pending["expires_at"] = (NOW[0] - timedelta(minutes=1)).isoformat()
    check("expire_pending reports the drop", c.expire_pending(), True)
    check("pendng cleared by expiry", c.pending, None)
    check("expire_pending is idempotent", c.expire_pending(), False)


async def test_one_shot_close_and_undo():
    reset_recorder()
    STORE_DATA.clear()
    c = make_coordinator({**BASE, "last_read_date": "2026-09-07"})
    await c.async_load_ledger()
    c.manual_kwh = 350.0
    await refresh(c)

    out = await c.async_close_period(force=True, confirm=True)
    check("close_period(confirm=True) writes in one call", out["confirmed"], True)
    check("two readings", readings(c), ["2026-09-07", "2026-10-14"])

    undone = await c.async_undo_close_period()
    check("undo reports success", undone["undone"], True)
    check("the wrong date is gone", readings(c), ["2026-09-07"])
    check("the removed row is returned", undone["removed"]["date"], "2026-10-14")
    check("the period is open again", c.period_start, date(2026, 9, 8))
    check("and it has not ended", c.period_has_ended, False)
    await araises("undo of an unknown date is refused", c.async_undo_close_period(date(2000, 1, 1)), "沒有")

    await araises("undoing an empty ledger is refused", make_coordinator(BASE).async_undo_close_period(), "空的")


async def test_add_update_set_last_read():
    reset_recorder()
    STORE_DATA.clear()
    c = make_coordinator({**BASE, "last_read_date": "2026-09-07"})
    await c.async_load_ledger()

    await araises("adding a duplicate date is refused", c.async_add_reading(date(2026, 9, 7)), "已經")
    overwritten = await c.async_add_reading(date(2026, 9, 7), actual_kwh=999.0, force=True)
    check("force overwrites the amounts", c.ledger.get_reading(date(2026, 9, 7))["kwh"], 999.0)
    check("overwrite is reported", overwritten["reading"]["kwh"], 999.0)

    await c.async_add_reading(date(2026, 11, 20))
    out = await c.async_update_reading(
        date(2026, 11, 20), new_date=date(2026, 11, 18), actual_bill=1234.0
    )
    check("the date moved", readings(c), ["2026-09-07", "2026-11-18"])
    check("the amounts followed", c.ledger.get_reading(date(2026, 11, 18))["bill"], 1234.0)
    check("changes are reported", out["changes"], ["金額=1234.0", "日期 2026-11-20 → 2026-11-18"])
    check("the cached computation was dropped by the move", c.ledger.get_reading(date(2026, 11, 18))["computed"], None)

    await c.async_update_reading(date(2026, 11, 18), clear_amounts=True)
    check("clear_amounts wipes the amounts", c.ledger.get_reading(date(2026, 11, 18))["bill"], None)
    await araises("rewriting an unknown date is refused", c.async_update_reading(date(2020, 1, 1)), "沒有")

    # a move onto an occupied date is refused
    await c.async_add_reading(date(2026, 12, 5))
    await araises(
        "moving onto an occupied date is refused",
        c.async_update_reading(date(2026, 12, 5), new_date=date(2026, 11, 18)),
        "已經有一筆",
    )

    # set_last_read_date keeps later dates unless history is reset
    kept = await c.async_set_last_read_date(date(2026, 9, 15))
    check("anchor added, later dates kept", readings(c), ["2026-09-15", "2026-11-18", "2026-12-05"])
    check("only the dates before the new anchor are dropped", kept["removed"], ["2026-09-07"])
    wiped = await c.async_set_last_read_date(date(2026, 10, 1), reset_history=True)
    check("reset_history wipes the rest", readings(c), ["2026-10-01"])
    ok("the wipe is reported", sorted(wiped["removed"]) == ["2026-09-15", "2026-11-18", "2026-12-05"], wiped["removed"])


async def test_period_report():
    reset_recorder()
    STORE_DATA.clear()
    cfg = {**BASE, "source_mode": "statistics", "source_entities": ["sensor.s"], "last_read_date": "2026-09-07"}
    c = make_coordinator(cfg)
    await c.async_load_ledger()
    await c.async_add_reading(date(2026, 9, 30))

    # 23 days of 10 kWh: 2026-09-08 .. 2026-09-30
    RECORDER_RESULT["sensor.s"] = daily_rows(*([10.0] * 23), first=(2026, 9, 8))

    report = await c.async_period_report(date(2026, 9, 30))
    check("report kwh is the window's own total", report["kwh"], 230.0)
    check("report source", report["source"], "statistics")
    check("report window", (report["period_start"], report["period_end"]), ("2026-09-08", "2026-09-30"))
    check("report days", report["days"], 23)
    check("report coverage start", report["coverage_start"], "2026-09-08")
    check("report last day", report["last_day"], "2026-09-30")
    ok("report carries the full bill detail", "tiers" in (report["bill"] or {}), sorted((report["bill"] or {}).keys())[:8])

    # an explicit past window can be recomputed even with no read dates around it
    windowed = await c.async_period_report(start=date(2026, 9, 10), end=date(2026, 9, 20))
    check("explicit window total", windowed["kwh"], 110.0)
    check("explicit window days", windowed["days"], 11)

    # the recorder call shape
    RECORDER_CALLS.clear()
    await c._statistics_kwh(date(2026, 9, 2), date(2026, 9, 30))
    call = RECORDER_CALLS[-1]
    check("end_time is the start of the end day, never end + 1", call["end"], datetime(2026, 9, 30, 0, 0, tzinfo=_TZ))
    check("start_time is the start of the start day", call["start"], datetime(2026, 9, 2, 0, 0, tzinfo=_TZ))
    check("period", call["period"], "day")
    check("types", call["types"], {"change", "sum"})
    check("units", call["units"], None)
    check("statistic ids", call["ids"], {"sensor.s"})

    # an empty recorder is explained rather than silently zero
    RECORDER_RESULT.clear()
    empty = await c.async_period_report(date(2026, 9, 30))
    check("empty window has no kwh", empty.get("kwh"), None)
    ok("empty window has a reason", "沒有回傳任何資料列" in (empty.get("reason") or ""), empty.get("reason"))

    # refusals
    await araises("reversed window is refused", c.async_period_report(start=date(2026, 9, 30), end=date(2026, 9, 1)), "不得晚於")
    await araises("a window with no read dates is refused", make_coordinator(cfg).async_period_report(), "找不到")
    STORE_DATA.clear()
    future = make_coordinator({**BASE, "last_read_date": "2026-09-07"})
    await future.async_load_ledger()
    await future.async_add_reading(date(2026, 11, 20))
    await araises("a window that has not ended is refused", future.async_period_report(), "還沒到")

    # all=True lists every window without touching the recorder
    RECORDER_RESULT["sensor.s"] = daily_rows(*([10.0] * 23), first=(2026, 9, 8))
    everything = await c.async_period_report(all_periods=True)
    check("all_periods count", everything["count"], 1)
    check("all_periods row", everything["periods"][0]["period_end"], "2026-09-30")


async def test_publish_statistics():
    """The external series is a whole-series rewrite of the finished periods."""
    reset_recorder()
    STORE_DATA.clear()
    cfg = {
        **BASE,
        "source_mode": "statistics",
        "source_entities": ["sensor.s"],
        "last_read_date": "2026-08-01",
    }
    c = make_coordinator(cfg)
    await c.async_load_ledger()
    await c.async_add_reading(date(2026, 9, 1))
    await c.async_add_reading(date(2026, 9, 30))

    # 2026-08-02 .. 2026-09-30, 10 kWh a day -> 310 + 290 = 600
    RECORDER_RESULT["sensor.s"] = daily_rows(*([10.0] * 60), first=(2026, 8, 2))
    computed = await c.async_backfill_periods()
    check("both finished windows backfilled", computed, 2)
    check("window 1 kwh", c.ledger.get_reading(date(2026, 9, 1))["computed"]["kwh"], 310.0)
    check("window 2 kwh", c.ledger.get_reading(date(2026, 9, 30))["computed"]["kwh"], 290.0)

    EXTERNAL_STATS.clear()
    published = await c.async_publish_statistics()
    check("two periods published", published, 2)
    check("two series written", len(EXTERNAL_STATS), 2)

    kwh_meta = EXTERNAL_STATS[0]["metadata"]
    check("kwh statistic_id derives from unique_id", kwh_meta["statistic_id"], "taipower_bill:b_house_period_kwh")
    check("source is the domain", kwh_meta["source"], "taipower_bill")
    check("kwh unit", kwh_meta["unit_of_measurement"], "kWh")
    check("kwh unit_class is a known converter", kwh_meta["unit_class"], "energy")
    check("mean_type is NONE (required in 2026.11)", kwh_meta["mean_type"], 0)
    check("has_sum", kwh_meta["has_sum"], True)
    check("has_mean", kwh_meta["has_mean"], False)

    kwh_rows = EXTERNAL_STATS[0]["statistics"]
    check("kwh row count", len(kwh_rows), 2)
    check("per-period values", [r["state"] for r in kwh_rows], [310.0, 290.0])
    check("sums are cumulative", [r["sum"] for r in kwh_rows], [310.0, 600.0])
    check("row 1 stamped at the day after the 抄見日", kwh_rows[0]["start"], datetime(2026, 9, 2, 0, 0, tzinfo=_TZ))
    check("row 2 stamped at the day after the 抄見日", kwh_rows[1]["start"], datetime(2026, 10, 1, 0, 0, tzinfo=_TZ))
    ok(
        "every row is tz-aware and on the top of the hour",
        all(
            r["start"].tzinfo is not None
            and r["start"].minute == 0
            and r["start"].second == 0
            and r["start"].microsecond == 0
            for r in kwh_rows
        ),
    )

    cost_meta = EXTERNAL_STATS[1]["metadata"]
    check("cost statistic_id", cost_meta["statistic_id"], "taipower_bill:b_house_period_cost")
    check("cost unit", cost_meta["unit_of_measurement"], "TWD")
    check("TWD has no unit_class", cost_meta["unit_class"], None)
    cost_rows = EXTERNAL_STATS[1]["statistics"]
    check("cost sums are the running total", cost_rows[1]["sum"], round(cost_rows[0]["state"] + cost_rows[1]["state"], 2))

    # republishing is idempotent (the recorder updates rows at an existing start)
    EXTERNAL_STATS.clear()
    await c.async_publish_statistics()
    check("republish sends the same series", [r["state"] for r in EXTERNAL_STATS[0]["statistics"]], [310.0, 290.0])

    # a window that ends today is deliberately not published yet
    await c.async_add_reading(date(2026, 10, 14))
    kwh_now, _cost_now = c._statistic_rows()
    check("a window ending today is left out", len(kwh_now), 2)

    # write failures surface on the sensors instead of vanishing
    EXTERNAL_STATS_ERROR[0] = RuntimeError("boom")
    check("a write failure publishes nothing", await c.async_publish_statistics(), 0)
    ok("the write error is surfaced", "boom" in (c.stats_write_error or ""), c.stats_write_error)
    EXTERNAL_STATS_ERROR[0] = None
    await c.async_publish_statistics()
    check("successful write clears the error", c.stats_write_error, None)


async def test_reset_and_baseline():
    reset_recorder()
    STORE_DATA.clear()
    c = make_coordinator({**BASE, "last_read_date": "2026-09-07"})
    await c.async_load_ledger()
    await c.async_add_reading(date(2026, 11, 20))
    c.ledger.data["baseline_kwh"] = 100.0
    c.ledger.data["baseline_date"] = "2026-09-08"

    out = await c.async_reset_ledger()
    check("readings cleared count", out["readings_cleared"], 2)
    check("baseline cleared", out["baseline_cleared"], True)
    check("the list is empty", readings(c), [])
    check("baseline gone", c.ledger.data.get("baseline_kwh"), None)
    check("statistics cleared too", out["statistics_cleared"], True)
    check("both statistic ids cleared", CLEARED_STATS[-1], ["taipower_bill:b_house_period_kwh", "taipower_bill:b_house_period_cost"])

    await c.async_add_reading(date(2026, 10, 1))
    kept = await c.async_reset_ledger(clear_readings=False, clear_baseline=True)
    check("readings kept", readings(c), ["2026-10-01"])
    check("nothing reported as cleared", kept["readings_cleared"], 0)
    check("no statistics cleared when the list is kept", kept.get("statistics_cleared"), None)


async def test_live_delta_rebaseline():
    reset_recorder()
    STORE_DATA.clear()
    cfg = {
        **BASE,
        "source_mode": "statistics",
        "source_entities": ["sensor.total"],
        "last_read_date": "2026-09-07",
    }
    c = make_coordinator(cfg)
    await c.async_load_ledger()
    c.hass.states._values["sensor.total"] = "1000.0"

    check("the first poll only captures the zero point", await c._resolve_kwh(), (0.0, "live-delta"))
    check("baseline value", c.ledger.data["baseline_kwh"], 1000.0)
    check("baseline is anchored to the period start", c.ledger.data["baseline_date"], "2026-09-08")

    c.hass.states._values["sensor.total"] = "1350.0"
    check("delta since the baseline", await c._resolve_kwh(), (350.0, "live-delta"))

    await c.async_add_reading(date(2026, 10, 14), force=True)
    c.hass.states._values["sensor.total"] = "1400.0"
    check("a new period must not inherit the old usage", await c._resolve_kwh(), (0.0, "live-delta"))
    check("baseline followed the window", c.ledger.data["baseline_date"], "2026-10-15")

    c.hass.states._values["sensor.total"] = "5.0"
    check("a backwards meter is treated as a replacement", await c._resolve_kwh(), (0.0, "live-delta-reset"))
    check("baseline follows the new value", c.ledger.data["baseline_kwh"], 5.0)


async def test_migration():
    reset_recorder()
    STORE_DATA.clear()
    c = make_coordinator({**BASE, "last_read_date": "2026-09-07"})
    key = f"taipower_bill_ledger.{c.entry.entry_id}"
    # Seeded in the shape HA's storage file actually has: a version envelope
    # around the payload.  A bare payload can never reach the migration hook, so
    # seeding one would exercise a path core cannot produce.
    STORE_DATA[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {
            "periods": [
                {
                    "period_start": "2026-07-08",
                    "period_end": "2026-09-07",
                    "days": 62,
                    "kwh": 620.0,
                    "energy_cost": 2900.0,
                    "surcharge": 0.0,
                    "fixed_fees": 0.0,
                    "tax": 100.0,
                    "estimated_total": 3100.0,
                    "actual_total": 3000.0,
                    "variance": 100.0,
                    "avg_price_per_kwh": 4.8,
                    "seasons": ["summer"],
                    "source": "statistics",
                    "forced": False,
                }
            ]
        },
    }
    await c.async_load_ledger()
    check("a v0.1 period becomes a reading", readings(c), ["2026-09-07"])
    row = c.ledger.get_reading(date(2026, 9, 7))
    check("migrated kwh", row["kwh"], 620.0)
    check("migrated 電費單金額 is actual_total", row["bill"], 3000.0)
    check("migrated marker", row["note"], "migrated:v0.1 period")
    check("migrated computed estimate is estimated_total", row["computed"]["estimated_total"], 3100.0)
    check("migrated seasons", row["computed"]["seasons"], ["summer"])
    check("the old key is gone", "periods" in c.ledger.data, False)
    check("the migrated row is cached, not recomputed", row["computed"]["computed_at"], None)

    STORE_DATA.clear()
    anchor = make_coordinator({**BASE, "last_read_date": "2026-09-07"})
    await anchor.async_load_ledger()
    check("the v0.1 option anchor becomes a reading", anchor.ledger.get_reading(date(2026, 9, 7))["note"], "migrated:options.last_read_date")

    key = f"taipower_bill_ledger.{anchor.entry.entry_id}"
    STORE_DATA[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"readings": [{"date": "2026-09-07", "kwh": 5.0}]},
    }
    again = make_coordinator({**BASE, "last_read_date": "2026-09-07"})
    await again.async_load_ledger()
    check("no duplicate is created from the anchor", len(again.readings), 1)
    check("the existing amount is kept", again.readings[0]["kwh"], 5.0)

    payload = {"periods": [{"period_end": "2026-01-01"}]}
    kept = await coordinator_mod._async_migrate_store(1, 1, payload)
    check("the storage migration keeps the payload", kept, payload)
    check("a None payload becomes a dict", await coordinator_mod._async_migrate_store(1, 0, None), {})

    # A payload already at the store's version must NOT reach the migration
    # hook -- core loads it verbatim (Store._async_load_data).
    STORE_DATA.clear()
    key = f"taipower_bill_ledger.{c.entry.entry_id}"
    STORE_DATA[key] = {
        "version": coordinator_mod.STORAGE_VERSION,
        "minor_version": 1,
        "key": key,
        "data": {"readings": [{"date": "2026-09-07", "kwh": 42.0, "note": "as-stored"}]},
    }
    current = make_coordinator({**BASE, "last_read_date": "2026-09-07"})
    await current.async_load_ledger()
    check(
        "a current-version payload is loaded verbatim",
        current.ledger.get_reading(date(2026, 9, 7)).get("note"),
        "as-stored",
    )
    await current.ledger.async_save()
    check(
        "the saved envelope carries the store version",
        STORE_DATA[key]["version"],
        coordinator_mod.STORAGE_VERSION,
    )

    # ...and a bare ``Store`` (no subclass override) has no hook at all.
    plain = Store(object(), coordinator_mod.STORAGE_VERSION, "taipower_bill_ledger.plain")
    try:
        await plain._async_migrate_func(1, 1, {})
        raised = None
    except NotImplementedError:
        raised = "NotImplementedError"
    check("a bare Store has no migration hook", raised, "NotImplementedError")


def test_sensor_declarations():
    """Declarations must be legal HA combinations, and complete."""
    legal = {
        "energy": (None, "total", "total_increasing"),
        "monetary": (None, "total"),
        "duration": None,  # any state class
        "date": (None,),
    }
    problems = []
    for d in const_mod.SENSOR_DESCRIPTIONS:
        allowed = legal.get(d.device_class)
        if allowed is not None and d.state_class not in allowed:
            problems.append(f"{d.key}:{d.device_class}/{d.state_class}")
        if d.device_class == "date" and d.native_unit_of_measurement is not None:
            problems.append(f"{d.key}:date with a unit")
    check("every (device_class, state_class) pair is legal", problems, [])

    keys = [d.key for d in const_mod.SENSOR_DESCRIPTIONS]
    check("sensor count", len(keys), 17)
    check("keys are unique", len(set(keys)), 17)
    check("every sensor has a value mapping", sorted(sensor_mod._VALUE_KEYS), sorted(keys))
    check(
        "only 本期度數/本期電費 are total",
        sorted(d.key for d in const_mod.SENSOR_DESCRIPTIONS if d.state_class == "total"),
        ["period_cost", "period_kwh"],
    )
    check(
        "date sensors",
        sorted(d.key for d in const_mod.SENSOR_DESCRIPTIONS if d.device_class == "date"),
        ["next_read_date", "pending_close_date"],
    )
    check("_DATE_KEYS matches the date sensors", sorted(sensor_mod._DATE_KEYS), ["next_read_date", "pending_close_date"])


def test_sensor_entities():
    c = make_coordinator({**BASE, "last_read_date": "2026-09-07"})
    c.data = {
        "label": "B戶",
        "kwh": 350.0,
        "cost": 1234.0,
        "period_start": date(2026, 9, 8),
        "period_end": date(2026, 11, 6),
        "next_read_date": date(2026, 11, 6),
        "last_read_date": date(2026, 9, 7),
        "suggested_read_date": date(2026, 10, 14),
        "pending_close_date": "2026-10-14",
        "history": [],
        "readings": [],
        "bill": None,
        "cycle_days": 60,
    }
    sensors = {d.key: sensor_mod.TaipowerBillSensor(c, d, c.entry) for d in const_mod.SENSOR_DESCRIPTIONS}

    reset = datetime(2026, 9, 8, 0, 0, tzinfo=_TZ)
    check("period_kwh value", sensors["period_kwh"].native_value, 350.0)
    check("period_kwh last_reset", sensors["period_kwh"].last_reset, reset)
    check("period_cost last_reset", sensors["period_cost"].last_reset, reset)
    check(
        "only the total sensors declare last_reset",
        [k for k, s in sensors.items() if s.last_reset is not None],
        ["period_kwh", "period_cost"],
    )

    broken = []
    for key, sensor in sensors.items():
        try:
            attrs = sensor.state_attributes
        except Exception as err:  # noqa: BLE001 - that is exactly what we test
            broken.append(f"{key}:{err}")
            continue
        if "period_start" not in attrs or "readings" not in attrs:
            broken.append(f"{key}:missing attributes")
    check("state_attributes is legal and complete on all 17", broken, [])

    # A HA 2026.10 date sensor formats its *state* itself: the platform calls
    # ``value.isoformat()``, so ``native_value`` must hand back a real ``date``.
    # v0.2.0 returned the already-formatted string and every date sensor died
    # with ``'str' object has no attribute 'isoformat'`` on a real instance.
    value = sensors["next_read_date"].native_value
    check("next_read_date is a real date", value, date(2026, 11, 6))
    check("...which formats itself for the state machine", value.isoformat(), "2026-11-06")
    # This one is fed a *string* by the coordinator, exercising the coercion.
    value = sensors["pending_close_date"].native_value
    check("pending_close_date is coerced to a real date", value, date(2026, 10, 14))
    check("...which formats itself too", value.isoformat(), "2026-10-14")
    check("a date sensor has no unit", sensors["next_read_date"].entity_description.native_unit_of_measurement, None)

    attrs = sensors["period_kwh"].extra_state_attributes
    missing = [
        key
        for key in (
            "label", "usage_source", "period_start", "period_end", "next_read_date", "last_read_date",
            "suggested_read_date", "days_elapsed", "days_in_period", "days_remaining", "cycle_days",
            "period_has_started", "period_has_ended", "period_note", "cost", "avg_daily_kwh",
            "projected_kwh", "projected_cost", "bill_variance", "config_warning", "config_issues",
            "history", "history_len", "last_period_kwh", "last_period_cost", "last_period_avg_price",
            "prev_period_kwh", "prev_period_cost", "readings", "pending_close", "pending_close_date",
            "pending_close_seconds_left", "actual_bill", "stats_coverage_start", "stats_last_day",
            "stats_error", "stats_write_error",
        )
        if key not in attrs
    ]
    check("the card's attribute contract holds", missing, [])
    check("date attributes are ISO strings", attrs["next_read_date"], "2026-11-06")


def test_payload_shapes():
    row = {
        "date": "2026-09-07",
        "kwh": 620.0,
        "bill": 3000.0,
        "forced": True,
        "note": "x",
        "computed": {"kwh": 620.0},
        "recorded_at": "2026-09-07T12:00:00+08:00",
    }
    brief = coordinator_mod._reading_brief(row)
    check("brief drops the cached computation", "computed" in brief, False)
    check("brief keeps the date", brief["date"], "2026-09-07")
    check("brief keeps the forced flag", brief["forced"], True)
    check("brief of None", coordinator_mod._reading_brief(None), None)
    # _reading_payload is the *full* row (the write services return it so the
    # caller can see what was stored); only the per-sensor brief is slimmed.
    check("payload keeps the full row", "computed" in coordinator_mod._reading_payload(row), True)

    c = make_coordinator(BASE)
    bill = coordinator_mod.compute_bill(620.0, date(2026, 7, 8), date(2026, 9, 7), c.billing_config)
    summary = coordinator_mod._bill_summary(bill)
    check("the cacheable summary drops the tier tables", ("tiers" in summary, "segments" in summary), (False, False))
    ok("summary has the estimated total", summary["estimated_total"] > 0, summary["estimated_total"])
    ok("summary lists the seasons", isinstance(summary["seasons"], list) and summary["seasons"], summary["seasons"])


def test_notify_actions():
    """The action string is the *only* thing a notification button sends back.

    The Companion App fires ``mobile_app_notification_action`` with nothing but
    ``event.data.action`` -- no service call, no payload, none of the other keys
    of the notification -- so the entry it belongs to has to be encoded in the
    string itself.  Home Assistant core does the same in its own blueprint
    (``CONFIRM_<context.id>``), which is why the format below is not arbitrary.
    """
    v = validators_mod
    entry = "01M4V592FM4D25H7BSC9NEPNB1"

    for kind in (v.ACTION_CONFIRM, v.ACTION_CANCEL, v.ACTION_UNDO):
        action = v.notify_action(kind, entry)
        check(f"{kind} round-trips", v.parse_notify_action(action), (kind, entry))

    # Split on the *first* underscore, so an entry id that contains one still
    # comes back whole rather than being truncated.
    check(
        "an entry id with an underscore survives",
        v.parse_notify_action("TAIPOWER_CONFIRM_a_b"),
        ("CONFIRM", "a_b"),
    )

    # Everything that is not ours must be ignored, not guessed at: the event is
    # shared with every other integration and with hand-written automations, and
    # acting on a stray CONFIRM_… would write to the ledger.
    for foreign in (
        None,
        "",
        "CONFIRM_abc",  # the core blueprint's own shape
        "DISMISS_abc",
        "TAIPOWER_",
        "TAIPOWER_CONFIRM_",  # no entry id
        "TAIPOWER_nope_abc",  # unknown kind
        "taipower_confirm_abc",  # our prefix is upper case, deliberately
        123,
    ):
        check(f"ignored: {foreign!r}", v.parse_notify_action(foreign), None)

    # notify targets: stored options and hand-written YAML differ on the domain.
    check(
        "full service name",
        v.normalise_notify_target("notify.mobile_app_iphone"),
        ("notify", "mobile_app_iphone"),
    )
    check(
        "bare name gets the notify domain",
        v.normalise_notify_target("mobile_app_iphone"),
        ("notify", "mobile_app_iphone"),
    )
    check("too many dots is refused", v.normalise_notify_target("a.b.c"), None)
    check("a non-string is refused", v.normalise_notify_target(7), None)
    check("absent option is no targets", v.normalise_notify_targets(None), [])
    check(
        "a bare string is one target",
        v.normalise_notify_targets("notify.mobile_app_x"),
        [("notify", "mobile_app_x")],
    )
    check(
        "the same target written two ways collapses",
        v.normalise_notify_targets(["notify.mobile_app_x", "mobile_app_x", "a.b.c"]),
        [("notify", "mobile_app_x")],
    )

    # The keys are re-exported by const, which is what the coordinator imports.
    check("const re-exports the notify option", const_mod.CONF_NOTIFY_TARGETS, "notify_targets")
    check("const re-exports the undo action", const_mod.ACTION_UNDO, "UNDO")


# ==========================================================================
# 5. runner
# ==========================================================================
ASYNC_TESTS = [
    test_read_date_model,
    test_on_the_read_day,
    test_config_issues,
    test_close_propose_confirm,
    test_cancel_and_expiry,
    test_one_shot_close_and_undo,
    test_add_update_set_last_read,
    test_period_report,
    test_publish_statistics,
    test_reset_and_baseline,
    test_live_delta_rebaseline,
    test_migration,
]

SYNC_TESTS = [
    test_sensor_declarations,
    test_sensor_entities,
    test_payload_shapes,
    test_notify_actions,
]


async def main():
    for test in ASYNC_TESTS:
        _reset_now()
        STORE_DATA.clear()  # every test starts with an empty ledger
        print(f"\n--- {test.__name__} ---")
        try:
            await test()
        except Exception as err:  # noqa: BLE001 - a crash is a failure, not a stop
            failures.append(f"{test.__name__} raised {type(err).__name__}: {err}")
            print(f"FAIL  {test.__name__} raised {type(err).__name__}: {err}")

    for test in SYNC_TESTS:
        _reset_now()
        STORE_DATA.clear()
        print(f"\n--- {test.__name__} ---")
        try:
            test()
        except Exception as err:  # noqa: BLE001
            failures.append(f"{test.__name__} raised {type(err).__name__}: {err}")
            print(f"FAIL  {test.__name__} raised {type(err).__name__}: {err}")

    print()
    if failures:
        print(f"FAILED: {len(failures)} of {passes[0]} checks")
        for name in failures:
            print(f"  - {name}")
        sys.exit(1)
    print(f"OK: all {passes[0]} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
