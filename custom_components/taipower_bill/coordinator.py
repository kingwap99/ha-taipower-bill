"""Data coordinator for Taipower Bill.

v0.2.0 -- the source of truth is a **list of actual meter-read dates**
(歷次實際抄見日), not a button press.

A 台電 billing period is defined by two consecutive 抄見日: it runs from
(previous read date + 1 day) to (this read date).  Storing the read dates
instead of "the last time somebody pressed 結算本期" fixes the class of bug that
v0.1.x could not fix: a press on the wrong day used to move the anchor to
``last_read + cycle_days``, so pressing late counted the extra days twice and
pressing early mixed a partial-day total with a full-period tier ladder.  Here
the anchor is whatever date you actually write, and the dates stay editable
after the fact (``update_reading`` / ``undo_close_period``).

Consequences worth knowing:

* **Periods roll over by date, on their own.**  Once a read date passes, 本期
  becomes the next window at midnight even if nobody pressed anything.  A read
  date in the *future* is legitimate and useful -- it is the 下次抄表日 printed
  on the 電費單 -- so the window never has to be guessed from ``cycle_days``.
* **Nothing is written without confirmation.**  ``close_period`` only *proposes*:
  it stores a pending proposal (visible on the sensors and in a persistent
  notification) which has to be confirmed, and it expires on its own.
* **Every period can be re-derived.**  Each window's kWh/費用 is recomputed from
  the recorder's long-term statistics and cached on the reading, so history can
  be rewritten (or recomputed) long after the fact.
* **Each closed period is published as an external long-term statistic**
  (``taipower_bill:...._period_kwh`` / ``_period_cost``) -- that is what makes
  「回看前幾個月」 visible in the statistics dashboard, independent of this
  integration's own attributes.

Usage is resolved through three paths, best available first:

1. ``statistics`` -- the recorder's daily sums for each configured source
   entity, differenced across the window.  Accurate and back-fills
   retroactively.
2. ``live delta`` -- current cumulative state of the sources minus a baseline
   captured when the period opened.  Used when the recorder has no rows yet.
3. ``manual`` -- the number you key in from the paper bill.

**Guarding against an unusable window.**  Everything is anchored on the read
dates.  If they are wrong (most commonly an empty list, or every date in the
future) the whole period window is meaningless.  In that case no bill is
computed, no baseline is written, and ``config_warning`` carries a
human-readable explanation that the card surfaces at the top.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

import homeassistant.util.dt as dt_util
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .billing import BillResult, BillingConfig, compute_bill
from .const import (
    CONF_BUSINESS_TAX_RATE,
    CONF_CYCLE_DAYS,
    CONF_FIXED_FEES_PER_PERIOD,
    CONF_LABEL,
    CONF_LAST_READ_DATE,
    CONF_MIN_CHARGE_PER_MONTH,
    CONF_PERIOD_MONTHS,
    CONF_SOURCE_ENTITIES,
    CONF_SOURCE_MODE,
    CONF_SUMMER_END,
    CONF_SUMMER_START,
    CONF_SURCHARGE_PER_KWH,
    CONF_TARIFF_REVISION,
    DEFAULT_CYCLE_DAYS,
    DEFAULT_MIN_CHARGE_PER_MONTH,
    DEFAULT_PERIOD_MONTHS,
    DOMAIN,
    SOURCE_MODE_MANUAL,
    STORAGE_PREFIX,
    STORAGE_VERSION,
    UNIT_TWD,
)
from .tariffs import REVISIONS
from .validators import parse_date

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(minutes=5)

#: How many periods are published on the sensors / to the card (newest first).
MAX_HISTORY = 36

#: Warn when the newest elapsed read date has not moved for this many cycles.
STALE_CYCLES = 2

#: How long a proposed 結算 stays open before it quietly expires.  A pending
#: proposal writes nothing, so it needs no cleanup beyond being forgotten.
PENDING_TTL = timedelta(minutes=15)

#: Storage schema version.  v1 stored ``{"periods": [...]}`` (event driven);
#: v2 stores ``{"readings": [...]}`` (data driven).  The migration itself lives
#: in ``TaipowerLedger.migrate`` so it can be unit-tested without HA.
STORAGE_VERSION_V2 = 2


def _parse_month_day(value: str | None, default: tuple[int, int]) -> tuple[int, int]:
    if not value:
        return default
    try:
        month, day = value.split("-")
        return (int(month), int(day))
    except (ValueError, AttributeError):
        return default


def _stat_row_day(raw: Any) -> date | None:
    """Local date of a statistics row's ``start``.

    The recorder returns a Unix timestamp; HA's own websocket layer rewrites the
    same field to milliseconds, and an older/newer build could hand back a
    datetime, so accept all three shapes rather than trusting one.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        stamp = raw if raw.tzinfo else raw.replace(tzinfo=dt_util.UTC)
        return dt_util.as_local(stamp).date()
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return None
    if ts > 1e11:  # milliseconds since the epoch
        ts /= 1000.0
    try:
        return dt_util.as_local(datetime.fromtimestamp(ts, tz=dt_util.UTC)).date()
    except (OverflowError, OSError, ValueError):
        return None


async def _async_migrate_store(
    old_major: int, old_minor: int, data: dict | None
) -> dict:
    """Storage v1 -> v2: keep the payload, let ``TaipowerLedger.migrate`` reshape it.

    v1 held ``{"periods": [closed billing periods]}`` keyed off a single
    ``last_read_date``; v2 holds ``{"readings": [抄見日]}``.  Both are reshaped
    from the same facts, so the migration deliberately does nothing clever here
    -- it keeps the old payload intact and the ledger converts it on load, which
    keeps the conversion testable without a running Home Assistant.
    """
    _LOGGER.info(
        "Taipower Bill ledger: storage v%s.%s -> v2, payload kept for migration",
        old_major,
        old_minor,
    )
    return data if isinstance(data, dict) else {}


class TaipowerLedger:
    """Persisted list of actual meter-read dates (歷次抄見日) for one meter.

    ``data`` shape (v2)::

        {
          "readings": [
            {
              "date": "2026-09-07",     # 實際抄見日; may be in the future
              "kwh": 1234.5,            # 電費單上的本期度數 (optional)
              "bill": 3100.0,           # 電費單上的應繳金額 (optional)
              "computed": {...},        # 由長期統計回算的快照 (optional)
              "forced": False,          # 是否由 force 寫入
              "note": "initial",        # 這筆是怎麼來的
              "recorded_at": "..."      # ISO timestamp
            }
          ],
          "baseline_kwh": None,
          "baseline_date": None
        }

    Everything the integration shows is derived from the read dates, so the only
    invariant this class has to protect is: **one reading per date, always
    sorted**.  Amounts (kwh/bill) are optional annotations copied off the paper
    bill for reconciliation; they are never used to define a window.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = Store(
            hass,
            STORAGE_VERSION,
            f"{STORAGE_PREFIX}.{entry_id}",
            async_migrate_func=_async_migrate_store,
        )
        self.data: dict[str, Any] = {
            "readings": [],
            "baseline_kwh": None,
            "baseline_date": None,
        }

    async def async_load(self) -> None:
        saved = await self._store.async_load()
        if isinstance(saved, dict):
            self.data.update(saved)

    async def async_save(self) -> None:
        await self._store.async_save(self.data)

    # -- readings ----------------------------------------------------------
    @property
    def readings(self) -> list[dict]:
        """The read dates, oldest first.  Returns the live list (callers that
        only read are fine; mutate through the methods below)."""
        readings = self.data.setdefault("readings", [])
        readings.sort(key=lambda r: str(r.get("date") or ""))
        return readings

    def get_reading(self, when: date) -> dict | None:
        iso = when.isoformat()
        for reading in self.readings:
            if reading.get("date") == iso:
                return reading
        return None

    def add_reading(
        self,
        when: date,
        kwh: float | None = None,
        bill: float | None = None,
        computed: dict | None = None,
        note: str | None = None,
        forced: bool = False,
    ) -> dict:
        """Insert (or replace) the reading at ``when``.  Returns the record."""
        existing = self.get_reading(when)
        if existing is not None:
            if kwh is not None:
                existing["kwh"] = float(kwh)
            if bill is not None:
                existing["bill"] = float(bill)
            if computed is not None:
                existing["computed"] = computed
            if note:
                existing["note"] = note
            existing["forced"] = bool(forced)
            return existing
        record = {
            "date": when.isoformat(),
            "kwh": None if kwh is None else float(kwh),
            "bill": None if bill is None else float(bill),
            "computed": computed,
            "forced": bool(forced),
            "note": note,
            "recorded_at": dt_util.now().isoformat(),
        }
        self.readings.append(record)
        self.readings.sort(key=lambda r: str(r.get("date") or ""))
        return record

    def remove_reading(self, when: date) -> dict | None:
        iso = when.isoformat()
        for reading in list(self.readings):
            if reading.get("date") == iso:
                self.readings.remove(reading)
                return reading
        return None

    def move_reading(self, when: date, new_date: date) -> dict | None:
        """Re-date a reading, keeping its amounts and recomputed block."""
        record = self.remove_reading(when)
        if record is None:
            return None
        record["date"] = new_date.isoformat()
        # A different date means a different window, so the cached computation
        # no longer describes it.
        record["computed"] = None
        self.readings.append(record)
        self.readings.sort(key=lambda r: str(r.get("date") or ""))
        return record

    def clear_computed(self) -> int:
        """Drop every cached computation (after the windows change shape)."""
        count = 0
        for reading in self.readings:
            if reading.get("computed") is not None:
                reading["computed"] = None
                count += 1
        return count

    def reset(
        self, clear_readings: bool = True, clear_baseline: bool = True
    ) -> dict:
        """Drop the recorded read dates and/or the live-delta baseline."""
        dropped = len(self.readings) if clear_readings else 0
        if clear_readings:
            self.data["readings"] = []
        if clear_baseline:
            self.data["baseline_kwh"] = None
            self.data["baseline_date"] = None
        return {"readings_cleared": dropped, "baseline_cleared": bool(clear_baseline)}

    def migrate(self, last_read_date: date | None) -> bool:
        """Reshape a v1 payload into readings.  Returns True when anything moved.

        Two legacy sources are folded into the list:

        * ``periods`` -- the closed periods v0.1.x recorded on every 結算本期
          press.  Their ``period_end`` *is* a read date, so each one becomes a
          reading that carries the amounts and the old computed figures.  That
          preserves the user's history across the upgrade.
        * ``options[last_read_date]`` -- the single anchor v0.1.x kept in the
          config entry.  It becomes the oldest reading when nothing else
          covers it.
        """
        changed = False
        readings = self.data.get("readings") or []
        if not isinstance(readings, list):
            readings = []
            changed = True
        known = {str(r.get("date")) for r in readings if isinstance(r, dict)}

        legacy_periods = self.data.pop("periods", None)
        if legacy_periods:
            changed = True
            for record in legacy_periods:
                if not isinstance(record, dict):
                    continue
                end = parse_date(record.get("period_end"))
                if end is None or end.isoformat() in known:
                    continue
                readings.append(
                    {
                        "date": end.isoformat(),
                        "kwh": record.get("kwh"),
                        "bill": record.get("actual_total"),
                        "computed": _computed_from_v1_period(record),
                        "forced": bool(record.get("forced")),
                        "note": "migrated:v0.1 period",
                        "recorded_at": None,
                    }
                )
                known.add(end.isoformat())

        if last_read_date is not None and last_read_date.isoformat() not in known:
            changed = True
            readings.append(
                {
                    "date": last_read_date.isoformat(),
                    "kwh": None,
                    "bill": None,
                    "computed": None,
                    "forced": False,
                    "note": "migrated:options.last_read_date",
                    "recorded_at": None,
                }
            )
            known.add(last_read_date.isoformat())

        readings.sort(key=lambda r: str(r.get("date") or ""))
        if readings != (self.data.get("readings") or []):
            changed = True
        self.data["readings"] = readings
        return changed


def _computed_from_v1_period(record: dict) -> dict | None:
    """Translate a v0.1 ledger row into the v0.2 ``computed`` block."""
    if record.get("estimated_total") is None and record.get("kwh") is None:
        return None
    return {
        "kwh": record.get("kwh"),
        "days": record.get("days"),
        "energy_cost": record.get("energy_cost"),
        "surcharge": record.get("surcharge"),
        "fixed_fees": record.get("fixed_fees"),
        "tax": record.get("tax"),
        "estimated_total": record.get("estimated_total"),
        "avg_price_per_kwh": record.get("avg_price_per_kwh"),
        "marginal_price_per_kwh": None,
        "seasons": record.get("seasons") or [],
        "source": record.get("source"),
        "coverage_start": None,
        "last_day": None,
        "computed_at": None,
    }


class TaipowerDataUpdateCoordinator(DataUpdateCoordinator):
    """Polls usage sources and produces the current period's bill."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.options: dict[str, Any] = {**entry.data, **entry.options}
        self.label: str = self.options.get(CONF_LABEL) or "Taipower"
        self.source_entities: list[str] = list(self.options.get(CONF_SOURCE_ENTITIES) or [])
        self.source_mode: str = self.options.get(CONF_SOURCE_MODE, "statistics")
        self.cycle_days: int = int(self.options.get(CONF_CYCLE_DAYS, DEFAULT_CYCLE_DAYS))
        self.period_months: int = int(self.options.get(CONF_PERIOD_MONTHS, DEFAULT_PERIOD_MONTHS))
        self.manual_kwh: float | None = None
        self.actual_bill: float | None = None
        self.ledger = TaipowerLedger(hass, entry.entry_id)
        self._logged_issues: set[str] = set()
        #: date of the first statistics row actually found for the window, when
        #: it is later than period_start (i.e. the window is only partly covered).
        self.stats_coverage_start: date | None = None
        #: last day the statistics rows reach (today's row is still partial).
        self.stats_last_day: date | None = None
        #: why the last recorder statistics read failed, if it did.  Surfaced on
        #: the sensors so a silent fall back to the live-delta path is visible.
        self.stats_error: str | None = None
        #: why the last write to the recorder's long-term statistics failed.
        self.stats_write_error: str | None = None
        #: A proposed-but-unconfirmed 結算.  Deliberately in memory only: a
        #: proposal that does not survive a restart cannot lose data, and an
        #: expired proposal simply disappears.  See ``PENDING_TTL``.
        self.pending: dict | None = None
        self._notification_id = f"{DOMAIN}_{entry.entry_id}_pending"
        #: True while our persistent notification for the pending 結算 is on
        #: screen, so we never try to dismiss one we did not create.
        self._notification_open = False
        #: Per-poll live figures for windows whose 抄見日 is today (see
        #: ``async_live_windows``); reset at the start of every poll.
        self._live_windows: dict[str, dict] = {}

        rev_key = self.options.get(CONF_TARIFF_REVISION)
        revisions = REVISIONS
        if rev_key and rev_key != "latest":
            chosen = tuple(r for r in REVISIONS if r.key == rev_key)
            revisions = chosen or REVISIONS

        self.billing_config = BillingConfig(
            period_months=self.period_months,
            cycle_days=self.cycle_days,
            min_charge_per_month=float(
                self.options.get(CONF_MIN_CHARGE_PER_MONTH, DEFAULT_MIN_CHARGE_PER_MONTH)
            ),
            summer_start=_parse_month_day(self.options.get(CONF_SUMMER_START), (6, 1)),
            summer_end=_parse_month_day(self.options.get(CONF_SUMMER_END), (9, 30)),
            surcharge_per_kwh=float(self.options.get(CONF_SURCHARGE_PER_KWH, 0.0) or 0.0),
            fixed_fees_per_period=float(self.options.get(CONF_FIXED_FEES_PER_PERIOD, 0.0) or 0.0),
            business_tax_rate=float(self.options.get(CONF_BUSINESS_TAX_RATE, 0.0) or 0.0),
            revisions=revisions,
        )

        super().__init__(hass, _LOGGER, name=f"{DOMAIN} {self.label}", update_interval=POLL_INTERVAL)

    # -- read dates (the source of truth) ----------------------------------
    @property
    def today(self) -> date:
        return dt_util.now().date()

    @property
    def readings(self) -> list[dict]:
        """Every 抄見日, oldest first."""
        return self.ledger.readings

    @staticmethod
    def reading_date(reading: dict | None) -> date | None:
        if not reading:
            return None
        return parse_date(reading.get("date"))

    @property
    def current_reading(self) -> dict | None:
        """The newest read date that has already happened (本期起算的那一筆)."""
        today = self.today
        found: dict | None = None
        for reading in self.readings:
            when = self.reading_date(reading)
            if when is not None and when <= today:
                found = reading
        return found

    @property
    def next_reading(self) -> dict | None:
        """The earliest read date still in the future, when one is known.

        This is usually the 下次抄表日 printed on the 電費單.  When it is present
        it *is* the end of the current period, which is strictly better than
        projecting ``cycle_days`` from the previous read.
        """
        today = self.today
        for reading in self.readings:
            when = self.reading_date(reading)
            if when is not None and when > today:
                return reading
        return None

    @property
    def last_read_date(self) -> date | None:
        return self.reading_date(self.current_reading)

    @property
    def period_start(self) -> date | None:
        """First day counted in the current bill = the day after 上期抄見日."""
        last_read = self.last_read_date
        return last_read + timedelta(days=1) if last_read else None

    @property
    def period_end(self) -> date | None:
        """The current period's read date: known if scheduled, else projected."""
        upcoming = self.next_reading
        if upcoming is not None:
            return self.reading_date(upcoming)
        last_read = self.last_read_date
        return last_read + timedelta(days=self.cycle_days) if last_read else None

    @property
    def next_read_date(self) -> date | None:
        return self.period_end

    @property
    def suggested_read_date(self) -> date:
        """The 抄見日 a 結算 press would record by default.

        The projected/known read date when it has already passed (this is the
        whole point: pressing three days late must not move the period three
        days forward), otherwise today.
        """
        end = self.period_end
        if end is not None and end <= self.today:
            return end
        return self.today

    @property
    def period_has_started(self) -> bool:
        start = self.period_start
        return start is not None and start <= self.today

    @property
    def period_has_ended(self) -> bool:
        end = self.period_end
        return end is not None and self.today >= end

    @property
    def days_elapsed(self) -> int:
        """Days of the window already counted, 0 when the period has not started."""
        start, end = self.period_start, self.period_end
        if start is None or end is None or start > self.today:
            return 0
        return (min(self.today, end) - start).days + 1

    @property
    def days_remaining(self) -> int | None:
        end = self.period_end
        return None if end is None else max(0, (end - self.today).days)

    # -- periods derived from the read dates -------------------------------
    def period_windows(self) -> list[tuple[date, date, dict, dict]]:
        """Every window implied by two consecutive read dates.

        Returns ``(start, end, start_reading, end_reading)`` where the window
        covers ``start``..``end`` inclusive -- ``start`` is the day after the
        previous 抄見日, ``end`` is the 抄見日 itself.
        """
        windows: list[tuple[date, date, dict, dict]] = []
        readings = self.readings
        for previous, current in zip(readings, readings[1:]):
            previous_date = self.reading_date(previous)
            end = self.reading_date(current)
            if previous_date is None or end is None:
                continue
            start = previous_date + timedelta(days=1)
            if end < start:
                continue
            windows.append((start, end, previous, current))
        return windows

    def closed_windows(self) -> list[tuple[date, date, dict, dict]]:
        """Windows whose 抄見日 has arrived, oldest first."""
        today = self.today
        return [window for window in self.period_windows() if window[1] <= today]

    def _period_record(
        self,
        start: date,
        end: date,
        current: dict,
        block: dict | None = None,
    ) -> dict:
        """One period as the sensors/card see it (never contains I/O).

        ``block`` overrides the cached computation -- used for the window whose
        抄見日 is today, which is recomputed live on every poll because today's
        daily statistics row is still being written.
        """
        computed = block if block is not None else (current.get("computed") or {})
        actual_total = current.get("bill")
        estimated = computed.get("estimated_total")
        variance = None
        if estimated is not None and actual_total is not None:
            variance = round(float(estimated) - float(actual_total), 2)
        return {
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "days": (end - start).days + 1,
            "kwh": computed.get("kwh"),
            "actual_kwh": current.get("kwh"),
            "energy_cost": computed.get("energy_cost"),
            "surcharge": computed.get("surcharge"),
            "fixed_fees": computed.get("fixed_fees"),
            "tax": computed.get("tax"),
            "estimated_total": estimated,
            "actual_total": actual_total,
            "variance": variance,
            "avg_price_per_kwh": computed.get("avg_price_per_kwh"),
            "marginal_price_per_kwh": computed.get("marginal_price_per_kwh"),
            "seasons": computed.get("seasons") or [],
            "source": computed.get("source"),
            "forced": bool(current.get("forced")),
            "computed_at": computed.get("computed_at"),
            "is_closed": end <= self.today,
            "partial": end >= self.today,
        }

    def history_records(self) -> list[dict]:
        """Closed periods, oldest first, capped at ``MAX_HISTORY``.

        Read from the cached per-period computation only -- this runs on every
        poll, so it must never touch the recorder.
        """
        records = [
            self._period_record(start, end, current, self._live_windows.get(end.isoformat()))
            for start, end, _previous, current in self.closed_windows()
        ]
        return records[-MAX_HISTORY:]

    # -- configuration sanity ---------------------------------------------
    @property
    def config_issues(self) -> list[str]:
        """Machine-readable list of things that make the numbers untrustworthy."""
        issues: list[str] = []
        if not self.readings:
            issues.append("no_readings")
        elif self.last_read_date is None:
            # Every read date is in the future, so nothing can be counted yet.
            issues.append("no_current_reading")
        elif (self.today - self.last_read_date).days > self.cycle_days * STALE_CYCLES:
            issues.append("last_read_stale")
        if self.source_mode != SOURCE_MODE_MANUAL and not self.source_entities:
            issues.append("no_source_entities")
        if self.cycle_days < 20:
            issues.append("cycle_days_suspicious")
        return issues

    def _config_warning(self) -> str | None:
        issues = self.config_issues
        if not issues:
            return None
        parts: list[str] = []
        if "no_readings" in issues:
            parts.append(
                "還沒有任何抄見日。請按「結算本期」（或呼叫服務 taipower_bill.add_reading）"
                "把電費單上的抄見日寫進來，帳期完全由這份清單決定。"
            )
        if "no_current_reading" in issues:
            earliest = next((self.reading_date(r) for r in self.readings if self.reading_date(r)), None)
            parts.append(
                f"清單裡的抄見日全都還在未來（最早的一筆是 {earliest}），本期無從起算，"
                f"所有數值都無法計算。請用 taipower_bill.set_last_read_date／update_reading "
                f"把它改成電費單上已經過去的抄見日。"
            )
        if "last_read_stale" in issues:
            parts.append(
                f"最後一筆已過的抄見日是 {self.last_read_date}（距今 "
                f"{(self.today - self.last_read_date).days} 天，超過 {STALE_CYCLES} 個週期）。"
                f"如果已經抄過表，請用「結算本期」或 taipower_bill.add_reading 補上新的抄見日；"
                f"若只是還沒收到電費單，可以忽略這則提醒。"
            )
        if "no_source_entities" in issues:
            parts.append("統計模式沒有設定任何 kWh 來源，請在「重新設定」補上。")
        if "cycle_days_suspicious" in issues:
            parts.append(f"抄表間隔只有 {self.cycle_days} 天，雙月帳單通常是 60 天。")
        return " ／ ".join(parts)

    def period_note(self) -> str | None:
        """A calm explanation for the one-day gap after a 抄見日."""
        start = self.period_start
        if self.last_read_date is not None and start is not None and start > self.today:
            return f"{self.last_read_date} 已完成抄表，新的一期從 {start} 起算，今天還沒有可統計的用電。"
        return None

    def _log_issues_once(self) -> None:
        for code in self.config_issues:
            if code in self._logged_issues:
                continue
            self._logged_issues.add(code)
            _LOGGER.error(
                "Taipower Bill '%s': 設定有問題 (%s) — 抄見日清單=%s, 今天=%s, 週期=%s 天. %s",
                self.label,
                code,
                [r.get("date") for r in self.readings] or "（空）",
                self.today,
                self.cycle_days,
                self._config_warning() or "",
            )

    # -- usage resolution --------------------------------------------------
    def _live_sum(self) -> float | None:
        total = 0.0
        seen = False
        for entity_id in self.source_entities:
            state = self.hass.states.get(entity_id)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            try:
                total += float(state.state)
            except (TypeError, ValueError):
                continue
            seen = True
        return total if seen else None

    async def _statistics_kwh(
        self, start: date, end: date
    ) -> tuple[float | None, date | None, date | None, str | None]:
        """Sum kWh across the sources from the recorder's daily statistics.

        Returns ``(kwh, first_day_found, last_day_found, error)``.  It has no
        side effects, so it can be reused for any historical window -- the
        current period and every closed one are computed by the same code path.
        ``first_day_found`` lets the caller tell the user when a window is only
        partly covered (e.g. the source entity was created after the period
        started) and ``last_day_found`` how fresh the figure is.

        Two details of the HA 2026.10 recorder API are easy to get wrong, and
        both were wrong here before:

        * ``statistics_during_period`` is a **module-level** function in
          ``homeassistant.components.recorder.statistics`` whose first argument
          is ``hass``.  It is not a method on the Recorder instance, and it must
          be dispatched to the executor because it opens a DB session.  HA core
          calls it exactly this way in ``recorder/websocket_api.py``.
        * ``end_time`` is rounded *up to the end of its own day*, so the day of
          ``end`` is already inside the window.  Passing ``end + timedelta(1)``
          silently adds one extra day.
        """
        if end < start or not self.source_entities:
            return None, None, None, None

        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import (
                statistics_during_period,
            )
        except ImportError as err:  # recorder unavailable/disabled
            return None, None, None, f"recorder 無法載入 ({err})"

        statistic_ids = set(self.source_entities)
        start_dt = dt_util.start_of_local_day(start)
        end_dt = dt_util.start_of_local_day(end)

        def _fetch() -> dict:
            return statistics_during_period(
                self.hass,
                start_dt,
                end_dt,
                statistic_ids,
                "day",
                None,
                {"change", "sum"},
            )

        try:
            rows_by_id = await get_instance(self.hass).async_add_executor_job(_fetch)
        except Exception as err:  # noqa: BLE001 - surfaced via stats_error
            return None, None, None, f"{type(err).__name__}: {err}"

        total = 0.0
        seen = False
        earliest: date | None = None
        latest: date | None = None
        for entity_id in self.source_entities:
            rows = (rows_by_id or {}).get(entity_id) or []
            if not rows:
                continue

            # Preferred: sum the per-row ``change`` values.  That counts the
            # first day of the window too, whereas ``sum[-1] - sum[0]``
            # silently drops it because a daily statistics row's ``sum`` is
            # cumulative *at the end* of that day.
            changes = [r.get("change") for r in rows]
            sums = [r.get("sum") for r in rows if r.get("sum") is not None]
            if all(c is not None for c in changes):
                total += sum(float(c) for c in changes)
                seen = True
            elif len(sums) >= 2:
                total += float(sums[-1]) - float(sums[0])
                seen = True
            elif len(sums) == 1:
                total += float(sums[0])
                seen = True
            else:
                continue

            for raw in (rows[0].get("start"), rows[-1].get("start")):
                day = _stat_row_day(raw)
                if day is None:
                    continue
                if earliest is None or day < earliest:
                    earliest = day
                if latest is None or day > latest:
                    latest = day

        if not seen:
            return None, None, None, "統計查詢沒有回傳任何資料列（來源可能還沒有長期統計）"

        return total, earliest, latest, None

    def _note_stats_error(self, message: str | None) -> None:
        """Remember why the statistics path failed, logging only on a change.

        The v0.1.0 version swallowed every failure in a ``debug`` line nobody
        reads, so a total API mismatch looked exactly like "no data yet".
        """
        if message == self.stats_error:
            return
        self.stats_error = message
        if message:
            _LOGGER.warning(
                "Taipower Bill '%s': 長期統計讀取失敗，暫時改用即時差值，度數會偏少：%s",
                self.label,
                message,
            )
        else:
            _LOGGER.info("Taipower Bill '%s': 長期統計讀取恢復正常", self.label)

    async def _resolve_kwh(self) -> tuple[float | None, str]:
        """Return (kwh, source_path_used) for the current, started window."""
        if self.source_mode == SOURCE_MODE_MANUAL:
            return (self.manual_kwh, "manual")

        start = self.period_start
        if start is None:
            return (None, "none")

        kwh, coverage, last_day, error = await self._statistics_kwh(start, self.today)
        self.stats_coverage_start = coverage
        self.stats_last_day = last_day
        self._note_stats_error(error)
        if kwh is not None and kwh >= 0:
            return (kwh, "statistics")

        live = self._live_sum()
        if live is None:
            return (None, "none")

        # The baseline belongs to one specific period: as soon as the window
        # moves (a new 抄見日 was written) the zero point has to be recaptured,
        # otherwise the next period would inherit the previous one's usage.
        baseline = self.ledger.data.get("baseline_kwh")
        baseline_date = self.ledger.data.get("baseline_date")
        anchor = start.isoformat()
        if baseline is None or baseline_date != anchor:
            self.ledger.data["baseline_kwh"] = live
            self.ledger.data["baseline_date"] = anchor
            await self.ledger.async_save()
            return (0.0, "live-delta")

        delta = live - float(baseline)
        if delta < 0:
            # Meter replaced, channel renamed, or integration re-added.
            _LOGGER.info(
                "%s: cumulative usage went backwards (%.2f -> %.2f); rebaselining",
                self.label,
                float(baseline),
                live,
            )
            self.ledger.data["baseline_kwh"] = live
            self.ledger.data["baseline_date"] = anchor
            await self.ledger.async_save()
            return (0.0, "live-delta-reset")
        return (delta, "live-delta")

    # -- 結算: propose, confirm, cancel -------------------------------------
    def _pending_expired(self, now: datetime | None = None) -> bool:
        if not self.pending:
            return False
        expires = dt_util.parse_datetime(str(self.pending.get("expires_at") or ""))
        if expires is None:
            return False
        return (now or dt_util.now()) >= expires

    def expire_pending(self) -> bool:
        """Forget a pending proposal that timed out.  Returns True if it went."""
        if self.pending is not None and self._pending_expired():
            _LOGGER.info("%s: 待確認的結算已逾時失效（沒有寫入任何資料）", self.label)
            self.pending = None
            return True
        return False

    def _pending_message(self) -> str:
        pending = self.pending or {}
        bill = pending.get("bill") or {}
        lines = [
            f"**台電 {self.label}** 有一筆待確認的結算：",
            f"- 帳期：{pending.get('period_start')} → {pending.get('read_date')}",
            f"- 本期度數：{pending.get('kwh')} kWh（來源：{pending.get('source')}）",
            f"- 推定電費：{bill.get('estimated_total')} 元",
        ]
        if pending.get("actual_bill") is not None:
            lines.append(f"- 電費單金額：{pending.get('actual_bill')} 元")
        if pending.get("force"):
            lines.append("- 這是強制結算（提前或未來日期）")
        lines.extend(
            [
                "",
                "要寫入請按「確認結算」按鈕、在卡片上按「確認寫入」，或呼叫服務 "
                "`taipower_bill.confirm_close`。",
                f"有效期限 {pending.get('expires_at')}；逾時自動失效，不會寫入任何資料。",
            ]
        )
        return "\n".join(lines)

    async def _notify_pending(self) -> None:
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": f"台電 {self.label}：待確認結算",
                    "message": self._pending_message(),
                    "notification_id": self._notification_id,
                },
                blocking=False,
            )
            self._notification_open = True
        except Exception as err:  # noqa: BLE001 - cosmetic only
            _LOGGER.warning("%s: 無法建立待確認通知：%s", self.label, err)

    async def _dismiss_pending_notification(self) -> None:
        if not self._notification_open:
            return
        self._notification_open = False
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "dismiss",
                {"notification_id": self._notification_id},
                blocking=False,
            )
        except Exception as err:  # noqa: BLE001 - cosmetic only
            _LOGGER.debug("%s: 無法關閉待確認通知：%s", self.label, err)

    def _assert_closeable(self, read_date: date, force: bool) -> None:
        """Refuse a 結算 that would corrupt the ledger; ``force`` overrides."""
        readings = self.readings
        last = self.last_read_date
        if not readings:
            if force:
                return
            raise HomeAssistantError(
                f"{self.label}: 抄見日清單還是空的，沒有上一筆可以接續。請先用服務 "
                f"taipower_bill.add_reading（或整合的「重新設定」）建立第一筆抄見日。"
            )
        if last is None:
            if force:
                return
            raise HomeAssistantError(
                f"{self.label}: 清單裡的抄見日全都還在未來"
                f"（{', '.join(str(r.get('date')) for r in readings)}），本期還沒起算。"
                f"請用 taipower_bill.set_last_read_date 改成已經過去的抄見日。"
            )
        if read_date <= last:
            raise HomeAssistantError(
                f"{self.label}: 抄見日 {read_date} 必須晚於清單中最後一筆已過的抄見日 {last}"
                f"（每一期是「上一筆抄見日的隔天」到「這一筆抄見日」）。"
            )
        if force:
            return
        if read_date > self.today:
            raise HomeAssistantError(
                f"{self.label}: 抄見日 {read_date} 是未來日期，結算只在記錄已經發生的抄表。"
                f"若只是想先記下電費單上的下次抄表日，請用 taipower_bill.add_reading。"
            )
        projected = self.period_end
        if projected is not None and projected > self.today and read_date < projected:
            raise HomeAssistantError(
                f"{self.label}: 推定抄見日 {projected} 還沒到（今天 {self.today}），"
                f"這次結算等於提前結算。若電費單上的抄見日確實比較早，請帶 force: true，"
                f"或用 taipower_bill.add_reading 直接寫入那個日期。"
            )

    async def async_propose_close(
        self,
        read_date: date | None = None,
        actual_kwh: float | None = None,
        actual_bill: float | None = None,
        force: bool = False,
    ) -> dict:
        """Propose a 結算 -- nothing is written until it is confirmed."""
        self._log_issues_once()
        wanted = read_date or self.suggested_read_date
        self._assert_closeable(wanted, force)

        # The 「電費單金額」 number entity feeds the proposal too, so a user who
        # only fills that in and presses the button gets it recorded.
        if actual_bill is None:
            actual_bill = self.actual_bill

        start = self.period_start
        if start is None:
            raise HomeAssistantError(
                f"{self.label}: 本期還沒有起算日，無法結算。請先確認抄見日清單。"
            )

        # Prefer the 度數 the caller passed (the paper bill).  In manual mode the
        # figure is ``manual_kwh`` itself, which is what the 度數 number entity
        # just wrote -- reading the polled snapshot instead would refuse a press
        # made in the seconds before the refresh it triggers has landed.
        kwh = actual_kwh
        if kwh is None and self.source_mode == SOURCE_MODE_MANUAL and self.manual_kwh is not None:
            kwh = self.manual_kwh
        if kwh is None:
            kwh = (self.data or {}).get("kwh")
        if kwh is None:
            raise HomeAssistantError(
                f"{self.label}: 沒有可用的本期度數（用量來源目前沒有資料）。"
                f"請先在手動模式填入度數，或呼叫服務時帶 actual_kwh（電費單上的度數）。"
            )

        bill = compute_bill(float(kwh), start, wanted, self.billing_config)
        now = dt_util.now()
        self.pending = {
            "read_date": wanted.isoformat(),
            "period_start": start.isoformat(),
            "kwh": round(float(kwh), 3),
            "actual_kwh": None if actual_kwh is None else round(float(actual_kwh), 3),
            "actual_bill": None if actual_bill is None else round(float(actual_bill), 2),
            "force": bool(force),
            "source": (self.data or {}).get("usage_source"),
            "bill": _bill_summary(bill),
            "proposed_at": now.isoformat(),
            "expires_at": (now + PENDING_TTL).isoformat(),
        }
        await self._notify_pending()
        await self.async_request_refresh()
        _LOGGER.info(
            "%s: 待確認結算 %s → %s（%s 度 / 推定 %s 元）；%s 前要確認，否則自動失效",
            self.label,
            start,
            wanted,
            self.pending["kwh"],
            self.pending["bill"].get("estimated_total"),
            self.pending["expires_at"],
        )
        return {"pending": True, **self.pending}

    async def async_confirm_close(self) -> dict:
        """Write the pending proposal into the ledger."""
        if self.pending is None:
            raise HomeAssistantError(
                f"{self.label}: 目前沒有待確認的結算。請先呼叫 taipower_bill.close_period "
                f"或按「結算本期」。"
            )
        if self._pending_expired():
            self.pending = None
            raise HomeAssistantError(
                f"{self.label}: 待確認的結算已經逾時失效（有效期限 15 分鐘），請重新提出一次。"
            )

        proposal = self.pending
        self.pending = None
        read_date = parse_date(proposal.get("read_date"))
        if read_date is None:
            raise HomeAssistantError(f"{self.label}: 待確認的結算缺少有效日期。")

        record = await self._write_reading(
            read_date,
            kwh=proposal.get("actual_kwh"),
            bill=proposal.get("actual_bill"),
            forced=bool(proposal.get("force")),
            note="close_period",
        )
        await self._dismiss_pending_notification()
        _LOGGER.info(
            "%s: 已結算 %s → %s（%s 度）；新的一期從 %s 起算",
            self.label,
            proposal.get("period_start"),
            read_date,
            proposal.get("kwh"),
            self.period_start,
        )
        return {
            "confirmed": True,
            "reading": _reading_payload(record),
            "readings": [r.get("date") for r in self.readings],
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
        }

    async def async_cancel_close(self) -> dict:
        """Drop the pending proposal (nothing was ever written)."""
        had = self.pending is not None
        self.pending = None
        await self._dismiss_pending_notification()
        if had:
            _LOGGER.info("%s: 已取消待確認的結算（沒有寫入任何資料）", self.label)
        return {"cancelled": had}

    async def async_close_period(
        self,
        read_date: date | None = None,
        actual_kwh: float | None = None,
        actual_bill: float | None = None,
        force: bool = False,
        confirm: bool = False,
    ) -> dict:
        """``close_period``: propose by default, write when ``confirm`` is true."""
        proposal = await self.async_propose_close(
            read_date=read_date,
            actual_kwh=actual_kwh,
            actual_bill=actual_bill,
            force=force,
        )
        if not confirm:
            return proposal
        return await self.async_confirm_close()

    # -- per-period computation (cache + backfill) -------------------------
    async def _compute_period_block(
        self, start: date, end: date, current: dict
    ) -> dict | None:
        """Recompute one window's numbers; no side effects beyond stats_error."""
        source = "statistics"
        kwh: float | None = None
        coverage: date | None = None
        last_day: date | None = None
        error: str | None = None

        if self.source_mode != SOURCE_MODE_MANUAL:
            kwh, coverage, last_day, error = await self._statistics_kwh(start, end)

        if kwh is None:
            if current.get("kwh") is None:
                if error:
                    self._note_stats_error(error)
                return None
            # The recorder cannot describe this window, so the 度數 printed on
            # the 電費單 is the best available number.  Using it beats leaving
            # the period blank.
            kwh = float(current["kwh"])
            source = "bill-kwh"

        bill = compute_bill(float(kwh), start, end, self.billing_config)
        block = _bill_summary(bill)
        block.update(
            {
                "kwh": round(float(kwh), 3),
                "days": bill.days,
                "source": source,
                "coverage_start": coverage.isoformat() if coverage else None,
                "last_day": last_day.isoformat() if last_day else None,
                "computed_at": dt_util.now().isoformat(),
            }
        )
        return block

    def _invalidate_after(self, when: date) -> int:
        """Drop cached numbers for the windows a change at ``when`` affects.

        The cache lives on the reading that *ends* a window, so a new date at
        ``when`` affects the window ending there (new) and the one ending at the
        following reading (whose start moved).
        """
        dropped = 0
        iso = when.isoformat()
        readings = self.readings
        for index, reading in enumerate(readings):
            if reading.get("date") == iso:
                for target in readings[index : index + 2]:
                    if target.get("computed") is not None:
                        target["computed"] = None
                        dropped += 1
                break
        return dropped

    async def async_backfill_periods(self, limit: int | None = None) -> int:
        """Recompute finished windows whose numbers are missing.

        ``limit`` caps the number of recorder queries (``None`` = no cap, used
        once in a background task at setup).  Stops as soon as a query comes back
        empty, so a broken statistics source cannot turn into a query storm.
        """
        computed = 0
        for start, end, _previous, current in self.period_windows():
            # A window whose 抄見日 is today cannot be cached: today's daily
            # statistics row is still being written, so the value would freeze
            # too low.  Those are computed live on every poll instead.
            if end >= self.today:
                continue
            if (current.get("computed") or {}).get("kwh") is not None:
                continue
            if limit is not None and computed >= limit:
                break
            block = await self._compute_period_block(start, end, current)
            if block is None:
                break
            current["computed"] = block
            computed += 1

        if computed:
            await self.ledger.async_save()
            await self.async_publish_statistics()
        return computed

    async def async_live_windows(self) -> dict[str, dict]:
        """Live figures for windows that end today (never cached)."""
        out: dict[str, dict] = {}
        today = self.today
        for start, end, _previous, current in self.closed_windows():
            if end != today:
                continue
            if (current.get("computed") or {}).get("kwh") is not None:
                continue
            block = await self._compute_period_block(start, end, current)
            if block is not None:
                out[end.isoformat()] = block
        return out

    # -- external long-term statistics -------------------------------------
    def _statistic_slug(self) -> str:
        """A stable, entity-id-safe slug for this entry's statistic ids.

        The config flow sets ``unique_id`` to ``taipower_bill.<slug>``, so
        stripping it out gives an id that survives a restart and does not change
        when the entry is renamed.
        """
        unique = str(getattr(self.entry, "unique_id", "") or "")
        slug = unique.split(".")[-1] if "." in unique else unique
        cleaned = "".join(
            char if (char.isalnum() and char.isascii()) or char == "_" else "_"
            for char in slug.lower()
        ).strip("_")
        if cleaned:
            return cleaned
        return f"meter_{str(self.entry.entry_id or 'unknown')[-6:].lower()}"

    def statistic_ids(self) -> tuple[str, str]:
        """External statistic ids for 每期用電 / 每期電費."""
        slug = self._statistic_slug()
        return f"{DOMAIN}:{slug}_period_kwh", f"{DOMAIN}:{slug}_period_cost"

    def _statistic_rows(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """(kWh rows, TWD rows) for every finished period, oldest first.

        Only periods whose 抄見日 has already passed are published: a window that
        ends today still has a partial statistics row, and publishing it would
        put a number that will never be corrected into the dashboard.  The next
        day it becomes ``end < today`` and gets published by the normal path.
        """
        kwh_rows: list[dict[str, Any]] = []
        cost_rows: list[dict[str, Any]] = []
        run_kwh = 0.0
        run_cost = 0.0
        today = self.today

        for _start, end, _previous, current in self.period_windows():
            if end >= today:
                continue
            block = current.get("computed") or {}
            if block.get("kwh") is None:
                continue

            value_kwh = float(block["kwh"])
            value_cost = float(block.get("estimated_total") or 0.0)
            run_kwh += value_kwh
            run_cost += value_cost

            # Row timestamps must be tz-aware and sit on the top of an hour
            # (the recorder checks minute == second == microsecond == 0 before
            # converting to UTC).  Local midnight of the day after the 抄見日 is
            # the moment the period closed, i.e. when its total became known.
            stamp = dt_util.start_of_local_day(end + timedelta(days=1))
            common: dict[str, Any] = {
                "start": stamp,
                "last_reset": None,
                "mean": None,
                "min": None,
                "max": None,
            }
            kwh_rows.append({**common, "state": round(value_kwh, 3), "sum": round(run_kwh, 3)})
            cost_rows.append({**common, "state": round(value_cost, 2), "sum": round(run_cost, 2)})

        return kwh_rows, cost_rows

    def _note_stats_write_error(self, message: str | None) -> None:
        if message == self.stats_write_error:
            return
        self.stats_write_error = message
        if message:
            _LOGGER.warning(
                "Taipower Bill '%s': 寫入長期統計失敗（整合本身仍可運作）：%s",
                self.label,
                message,
            )
        else:
            _LOGGER.info("Taipower Bill '%s': 長期統計寫入恢復正常", self.label)

    async def async_publish_statistics(self) -> int:
        """(Re)publish the per-period series as external long-term statistics.

        Re-importing a row at a start timestamp that already exists *updates* it
        (``_statistics_exists`` -> ``_update_statistics`` in the recorder), so
        rewriting the whole series is idempotent -- which is what makes
        「改寫／重算已結算期別」 show up correctly in the statistics dashboard
        instead of being appended as a second series.
        """
        kwh_rows, cost_rows = self._statistic_rows()
        if not kwh_rows:
            self._note_stats_write_error(None)
            return 0

        try:
            from homeassistant.components.recorder.statistics import (
                async_add_external_statistics,
            )
        except ImportError as err:  # recorder not set up
            self._note_stats_write_error(f"recorder 無法載入 ({err})")
            return 0

        # ``mean_type`` (and, before it, ``has_mean``) is required in the
        # metadata; omitting it only produces a deprecation warning today and
        # breaks in 2026.11, so keep both spellings in sync.
        try:
            from homeassistant.components.recorder.models import StatisticMeanType
        except ImportError:  # pragma: no cover - older/newer core layout
            try:
                from homeassistant.components.recorder.statistics import (
                    StatisticMeanType,
                )
            except ImportError:
                StatisticMeanType = None  # type: ignore[assignment]
        mean_type = getattr(StatisticMeanType, "NONE", 0)

        kwh_id, cost_id = self.statistic_ids()
        name = f"台電 {self.label}"
        series = (
            (kwh_id, f"{name} 每期用電", "kWh", "energy", kwh_rows),
            # TWD has no unit converter, so unit_class is None (not "monetary").
            (cost_id, f"{name} 每期電費", UNIT_TWD, None, cost_rows),
        )

        for statistic_id, series_name, unit, unit_class, rows in series:
            metadata = {
                "has_mean": False,
                "mean_type": mean_type,
                "has_sum": True,
                "name": series_name,
                "source": DOMAIN,
                "statistic_id": statistic_id,
                "unit_class": unit_class,
                "unit_of_measurement": unit,
            }
            try:
                async_add_external_statistics(self.hass, metadata, rows)
            except Exception as err:  # noqa: BLE001 - reported on the sensors
                self._note_stats_write_error(f"{type(err).__name__}: {err}")
                return 0

        self._note_stats_write_error(None)
        _LOGGER.debug(
            "%s: 已發佈 %s 期外部統計到 %s / %s",
            self.label,
            len(kwh_rows),
            kwh_id,
            cost_id,
        )
        return len(kwh_rows)

    # -- polling ------------------------------------------------------------
    def pending_payload(self) -> dict | None:
        """The open proposal (with the seconds left to confirm), else None."""
        if self.pending is None or self._pending_expired():
            return None
        payload = dict(self.pending)
        expires = dt_util.parse_datetime(str(payload.get("expires_at") or ""))
        if expires is not None:
            payload["seconds_left"] = max(
                0, int((expires - dt_util.now()).total_seconds())
            )
        return payload

    async def _async_update_data(self) -> dict[str, Any]:
        """Assemble everything the entities and the card read.

        A broken configuration never raises here: raising would leave every
        entity ``unavailable`` and hide the explanation.  Instead the window is
        refused, no baseline is written, and ``config_warning`` carries a
        readable reason the card shows at the top.
        """
        self._live_windows = {}
        if self.expire_pending():
            await self._dismiss_pending_notification()

        kwh: float | None = None
        source = "period-not-started"
        if self.period_has_started:
            kwh, source = await self._resolve_kwh()

        self._live_windows = await self.async_live_windows()
        history = self.history_records()

        start = self.period_start
        end = self.period_end
        days_in_period = (end - start).days + 1 if start and end else None
        days_elapsed = self.days_elapsed

        bill: BillResult | None = None
        if kwh is not None and start is not None and end is not None:
            # The live bill covers only the elapsed part of the window; the full
            # window (with the projected read date) is published separately as
            # period_start/period_end, and BillResult.as_attributes() uses
            # bill_window_* names precisely so the two cannot be confused.
            elapsed_end = min(end, self.today)
            if elapsed_end >= start:
                bill = compute_bill(float(kwh), start, elapsed_end, self.billing_config)

        avg_daily = None
        if kwh is not None and days_elapsed > 0:
            avg_daily = float(kwh) / days_elapsed

        projected_kwh = None
        projected_cost = None
        if avg_daily is not None and days_in_period:
            projected_kwh = avg_daily * days_in_period
            projected = compute_bill(
                projected_kwh, start, end, self.billing_config
            )
            projected_cost = projected.total

        # 「上期對帳」: the newest closed period that has both an estimate and the
        # amount printed on the 電費單.
        bill_variance = next(
            (record["variance"] for record in reversed(history) if record["variance"] is not None),
            None,
        )

        # 「上一期／上上期」: the two most recent closed periods, published as
        # their own sensors so a dashboard does not have to dig into the history
        # list to answer 「上個月多少錢」.
        last = history[-1] if history else None
        before = history[-2] if len(history) > 1 else None

        def _best_cost(record: dict | None) -> float | None:
            """The amount to show for a closed period: the paper bill if known."""
            if not record:
                return None
            if record.get("actual_total") is not None:
                return record["actual_total"]
            return record.get("estimated_total")

        pending = self.pending_payload()

        data: dict[str, Any] = {
            "label": self.label,
            "kwh": kwh,
            "cost": bill.total if bill is not None else None,
            "usage_source": source,
            # Kept as ``date`` objects: the sensor calls .isoformat() on them and
            # the card gets plain strings through the attributes layer.
            "period_start": start,
            "period_end": end,
            "next_read_date": self.next_read_date,
            "last_read_date": self.last_read_date,
            "suggested_read_date": self.suggested_read_date,
            "days_elapsed": days_elapsed,
            "days_in_period": days_in_period,
            "days_remaining": self.days_remaining,
            "avg_daily_kwh": avg_daily,
            "projected_kwh": projected_kwh,
            "projected_cost": projected_cost,
            "avg_price_per_kwh": bill.avg_price_per_kwh if bill else None,
            "marginal_price_per_kwh": bill.marginal_price_per_kwh if bill else None,
            "bill_variance": bill_variance,
            "history_len": len(history),
            "history": history,
            "last_period_start": last["period_start"] if last else None,
            "last_period_end": last["period_end"] if last else None,
            "last_period_days": last["days"] if last else None,
            "last_period_kwh": last["kwh"] if last else None,
            "last_period_cost": _best_cost(last),
            "last_period_estimated_cost": last["estimated_total"] if last else None,
            "last_period_actual_cost": last["actual_total"] if last else None,
            "last_period_variance": last["variance"] if last else None,
            "last_period_avg_price": last["avg_price_per_kwh"] if last else None,
            "last_period_source": last["source"] if last else None,
            "prev_period_start": before["period_start"] if before else None,
            "prev_period_end": before["period_end"] if before else None,
            "prev_period_kwh": before["kwh"] if before else None,
            "prev_period_cost": _best_cost(before),
            "prev_period_avg_price": before["avg_price_per_kwh"] if before else None,
            "pending_close": pending,
            "pending_close_date": (pending or {}).get("read_date"),
            "pending_close_seconds_left": (pending or {}).get("seconds_left"),
            "readings": [_reading_brief(r) for r in self.readings],
            # The amount keyed into the 「電費單金額」 number entity, so the card
            # can show what will be attached at the next 結算.
            "actual_bill": self.actual_bill,
            "bill": bill,
            "period_has_started": self.period_has_started,
            "period_has_ended": self.period_has_ended,
            "period_note": self.period_note(),
            "config_issues": self.config_issues,
            "config_warning": self._config_warning(),
            "stats_coverage_start": self.stats_coverage_start,
            "stats_last_day": self.stats_last_day,
            "stats_error": self.stats_error,
            "stats_write_error": self.stats_write_error,
            "cycle_days": self.cycle_days,
        }
        return data

    # -- writes -------------------------------------------------------------
    def _invalidate_from(self, when: date) -> int:
        """Drop cached numbers for every window that ends after ``when``.

        Used when the *shape* of the list changes (a reading removed or moved):
        every later window then describes a different span, so its cached kWh
        and 費用 are no longer true.
        """
        dropped = 0
        for reading in self.readings:
            day = self.reading_date(reading)
            if day is not None and day > when and reading.get("computed") is not None:
                reading["computed"] = None
                dropped += 1
        return dropped

    async def async_load_ledger(self) -> None:
        """Load the ledger and fold any v0.1 data into the read-date list.

        Must run *before* the first refresh: the read dates are what the whole
        period window is derived from, so refreshing first would compute a
        window from an empty list and log a spurious configuration error.
        """
        await self.ledger.async_load()
        if self.ledger.migrate(parse_date(self.options.get(CONF_LAST_READ_DATE))):
            await self.ledger.async_save()
            _LOGGER.info(
                "Taipower Bill '%s': 已把 v0.1 的歷史併入抄見日清單：%s",
                self.label,
                [r.get("date") for r in self.readings],
            )

    async def async_prepare(self) -> None:
        """Back-fill every finished window and publish the series (once).

        Runs in the background from ``async_setup_entry``, so a slow recorder or
        a broken statistics source can never hold up the entities loading.
        """
        # Publish the whole series once, so an upgrade from v0.1.x lands in the
        # statistics dashboard even when no new 結算 happens today.
        computed = await self.async_backfill_periods()
        if not computed:
            await self.async_publish_statistics()
        await self.async_request_refresh()

    async def _write_reading(
        self,
        read_date: date,
        kwh: float | None = None,
        bill: float | None = None,
        forced: bool = False,
        note: str | None = None,
    ) -> dict:
        """Write one 抄見日 and refresh everything derived from the list."""
        if read_date is None:
            raise HomeAssistantError(f"{self.label}: 需要一個抄見日。")

        record = self.ledger.add_reading(
            read_date, kwh=kwh, bill=bill, note=note, forced=forced
        )
        # The window ending here is new and the one after it (if any) now starts
        # a day later, so both cached blocks are stale.
        self._invalidate_after(read_date)
        # A previous configuration problem may be resolved now; let the next
        # problem be logged again rather than staying silent.
        self._logged_issues.clear()

        # Recompute the affected windows straight away -- this is a
        # user-initiated write, so a couple of recorder queries are affordable,
        # and the history is then correct the moment the service returns.
        await self.async_backfill_periods()
        await self.ledger.async_save()

        # The numbers the user keyed in have now been consumed by this period.
        if self.manual_kwh is not None:
            self.manual_kwh = 0.0
        self.actual_bill = None

        await self.async_publish_statistics()
        await self.async_request_refresh()
        return record

    async def async_add_reading(
        self,
        read_date: date,
        actual_kwh: float | None = None,
        actual_bill: float | None = None,
        force: bool = False,
        note: str = "add_reading",
    ) -> dict:
        """Add a 抄見日 exactly as given -- no confirmation step.

        This is the primitive behind 「清單為準」.  Use it for the 下次抄表日
        printed on the 電費單 (a future date is fine and simply replaces the
        ``cycle_days`` projection), for back-filling a date that was read but
        never written, or for repairing a wrong one.
        """
        if read_date is None:
            raise HomeAssistantError(f"{self.label}: 需要一個抄見日。")
        existing = self.ledger.get_reading(read_date)
        if existing is not None and not force:
            raise HomeAssistantError(
                f"{self.label}: {read_date} 已經是清單裡的抄見日。"
                f"要改金額／度數請用 taipower_bill.update_reading；要覆蓋請帶 force: true。"
            )

        record = await self._write_reading(
            read_date,
            kwh=actual_kwh,
            bill=actual_bill,
            forced=bool(force),
            note=note,
        )
        _LOGGER.info(
            "%s: 寫入抄見日 %s（%s）；清單=%s",
            self.label,
            read_date,
            "覆蓋既有" if existing is not None else "新增",
            [r.get("date") for r in self.readings],
        )
        return self._snapshot(record)

    async def async_undo_close_period(self, when: date | None = None) -> dict:
        """Remove the newest elapsed 抄見日, re-opening the period it closed.

        This is the 「不小心按到」 rescue: the wrong date simply disappears from
        the list, so the period it ended goes back to being the current one.  It
        returns the removed record, so the same date can be re-added later.
        """
        readings = self.readings
        if not readings:
            raise HomeAssistantError(f"{self.label}: 抄見日清單是空的，沒有東西可以撤銷。")

        target = when or self.last_read_date
        if target is None:
            # Every read date is in the future, so nothing has been closed yet.
            raise HomeAssistantError(
                f"{self.label}: 清單裡的抄見日全都還在未來"
                f"（{', '.join(str(r.get('date')) for r in readings)}），沒有已結算的期別可以撤銷。"
                f"要清掉某一筆請指定 date。"
            )

        removed = self.ledger.remove_reading(target)
        if removed is None:
            raise HomeAssistantError(
                f"{self.label}: 清單裡沒有 {target} 這筆抄見日"
                f"（目前有 {', '.join(str(r.get('date')) for r in readings)}）。"
            )

        # Every window that ends after the removed date now spans differently.
        self._invalidate_from(target)
        if self.pending is not None:
            self.pending = None
            await self._dismiss_pending_notification()

        await self.async_backfill_periods()
        await self.ledger.async_save()
        await self.async_publish_statistics()
        await self.async_request_refresh()
        _LOGGER.info(
            "%s: 已撤銷抄見日 %s；本期回到 %s → %s",
            self.label,
            target,
            self.period_start,
            self.period_end,
        )
        return {
            "undone": True,
            "removed": _reading_payload(removed),
            "readings": [r.get("date") for r in self.readings],
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "period_has_ended": self.period_has_ended,
        }

    async def async_update_reading(
        self,
        when: date,
        new_date: date | None = None,
        actual_kwh: float | None = None,
        actual_bill: float | None = None,
        recompute: bool = False,
        clear_amounts: bool = False,
    ) -> dict:
        """Rewrite an already-recorded 抄見日 (date, 度數, 金額) and recompute.

        ``recompute`` is accepted for compatibility; a rewrite always invalidates
        and recomputes the affected windows, because the cached kWh is derived
        from the window and the amounts.
        """
        if when is None:
            raise HomeAssistantError(f"{self.label}: 需要指定要改寫的抄見日。")

        reading = self.ledger.get_reading(when)
        if reading is None:
            raise HomeAssistantError(
                f"{self.label}: 清單裡沒有 {when} 這筆抄見日"
                f"（目前有 {', '.join(str(r.get('date')) for r in self.readings) or '（空）'}）。"
            )

        changes: list[str] = []
        if clear_amounts:
            reading["kwh"] = None
            reading["bill"] = None
            changes.append("清空度數／金額")
        if actual_kwh is not None:
            reading["kwh"] = float(actual_kwh)
            changes.append(f"度數={actual_kwh}")
        if actual_bill is not None:
            reading["bill"] = float(actual_bill)
            changes.append(f"金額={actual_bill}")

        if new_date is not None and new_date != when:
            if new_date != when and self.ledger.get_reading(new_date) is not None:
                raise HomeAssistantError(
                    f"{self.label}: {new_date} 已經有一筆抄見日了。"
                    f"要交換日期請先把其中一筆撤銷。"
                )
            self.ledger.move_reading(when, new_date)
            changes.append(f"日期 {when} → {new_date}")
            when = new_date

        # Always recompute: the window changed, or the amounts feeding the
        # fallback path did.
        self._invalidate_from(when)
        if reading.get("computed") is not None:
            reading["computed"] = None

        await self.ledger.async_save()
        await self.async_backfill_periods()
        await self.async_publish_statistics()
        await self.async_request_refresh()
        _LOGGER.info(
            "%s: 改寫抄見日 %s（%s）",
            self.label,
            when,
            "、".join(changes) or "重新計算",
        )
        return self._snapshot(reading, extra={"updated": when.isoformat(), "changes": changes})

    def _find_window(self, when: date | None) -> tuple[date, date, dict, dict] | None:
        windows = self.period_windows()
        if not windows:
            return None
        if when is None:
            closed = [window for window in windows if window[1] <= self.today]
            return closed[-1] if closed else windows[-1]
        for window in windows:
            if window[1] == when:
                return window
        for window in windows:
            if window[0] <= when <= window[1]:
                return window
        return None

    async def async_period_report(
        self,
        when: date | None = None,
        start: date | None = None,
        end: date | None = None,
        all_periods: bool = False,
    ) -> dict:
        """Recompute any window on demand -- 「任意期間回算明細」.

        Unlike the cached history this reads the recorder directly, so it works
        for a window from months ago whether or not it was ever 結算.
        """
        if all_periods:
            records = [
                self._period_record(
                    window_start,
                    window_end,
                    current,
                    self._live_windows.get(window_end.isoformat()),
                )
                for window_start, window_end, _previous, current in self.period_windows()
            ]
            return {"periods": records, "count": len(records), "today": self.today.isoformat()}

        if start is not None and end is not None:
            if end < start:
                raise HomeAssistantError(
                    f"{self.label}: 起始日 {start} 不得晚於結束日 {end}。"
                )
            current: dict = {}
        else:
            found = self._find_window(when)
            if found is None:
                raise HomeAssistantError(
                    f"{self.label}: 找不到對應的帳期。請先寫入抄見日，或用 start/end 指定一段期間"
                    f"（目前清單：{', '.join(str(r.get('date')) for r in self.readings) or '（空）'}）。"
                )
            start, end, _previous, current = found

        if end > self.today:
            raise HomeAssistantError(
                f"{self.label}: {end} 還沒到，無法回算將來的期間（今天 {self.today}）。"
            )

        block = await self._compute_period_block(start, end, current)
        if block is None:
            return {
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
                "days": (end - start).days + 1,
                "kwh": None,
                "reason": self.stats_error
                or "這個區間在長期統計裡沒有資料（來源的統計可能還不涵蓋那段時間）。",
            }

        detail = compute_bill(
            float(block["kwh"]), start, end, self.billing_config
        ).as_attributes()
        return {
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "days": (end - start).days + 1,
            "kwh": block["kwh"],
            "estimated_total": block.get("estimated_total"),
            "source": block.get("source"),
            "coverage_start": block.get("coverage_start"),
            "last_day": block.get("last_day"),
            "reading": _reading_payload(current) if current else None,
            "bill": detail,
        }

    async def async_set_last_read_date(
        self, read_date: date, reset_history: bool = False
    ) -> dict:
        """Set the anchor 抄見日 directly (the v0.1 service name, kept working).

        Later read dates are *kept* unless ``reset_history`` is set, so the
        original use -- 「上期抄見日填錯了，改成正確的」 -- does not silently
        throw away closed periods.
        """
        if read_date is None:
            raise HomeAssistantError(f"{self.label}: 需要一個日期。")

        removed: list[str] = []
        for reading in list(self.readings):
            day = self.reading_date(reading)
            if day is None or day == read_date:
                continue
            if reset_history or day < read_date:
                self.ledger.remove_reading(day)
                removed.append(day.isoformat())

        existing = self.ledger.get_reading(read_date)
        if existing is None:
            self.ledger.add_reading(read_date, note="set_last_read_date")

        # A different anchor changes the first window and every later one.
        self._invalidate_from(read_date)
        await self.ledger.async_save()
        await self.async_backfill_periods()
        await self.async_publish_statistics()
        await self.async_request_refresh()
        _LOGGER.info(
            "%s: 起始抄見日設為 %s（%s）",
            self.label,
            read_date,
            "並清除其他歷史" if reset_history else f"移除 {removed or '無'}",
        )
        return {
            "readings": [r.get("date") for r in self.readings],
            "last_read_date": self.last_read_date.isoformat() if self.last_read_date else None,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "removed": removed,
        }

    async def async_reset_ledger(
        self, clear_readings: bool = True, clear_baseline: bool = True
    ) -> dict:
        """Wipe the ledger (and, when the readings go, the published series)."""
        result = self.ledger.reset(
            clear_readings=clear_readings, clear_baseline=clear_baseline
        )
        self.pending = None
        await self._dismiss_pending_notification()
        await self.ledger.async_save()

        if clear_readings:
            result["statistics_cleared"] = await self._async_clear_statistics()

        await self.async_request_refresh()
        _LOGGER.info(
            "%s: 已清除 %s 筆抄見日（baseline %s）",
            self.label,
            result.get("readings_cleared"),
            "已清除" if clear_baseline else "保留",
        )
        return result

    async def _async_clear_statistics(self) -> bool:
        """Drop this entry's external statistics, so no stale series is left.

        Without this, wiping the ledger would leave the old 每期用電/每期電費
        series in the statistics dashboard forever, telling a story that no
        longer matches the integration.
        """
        try:
            from homeassistant.components.recorder.statistics import (
                async_clear_statistics,
            )
        except ImportError:
            return False
        kwh_id, cost_id = self.statistic_ids()
        try:
            async_clear_statistics(self.hass, [kwh_id, cost_id])
        except Exception as err:  # noqa: BLE001 - reported on the sensors
            self._note_stats_write_error(f"清除統計失敗 {type(err).__name__}: {err}")
            return False
        self._note_stats_write_error(None)
        return True

    def _snapshot(self, record: dict | None = None, extra: dict | None = None) -> dict:
        """The standard reply shape for the write services."""
        payload: dict[str, Any] = {
            "label": self.label,
            "readings": [r.get("date") for r in self.readings],
            "reading": _reading_payload(record),
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "days_elapsed": self.days_elapsed,
        }
        payload.update(extra or {})
        return payload


def _bill_summary(bill: BillResult) -> dict[str, Any]:
    """The slim, cacheable figures for one finished period.

    Deliberately drops ``segments``/``tiers``: they are the biggest part of the
    payload and the per-entity state-attribute budget is about 16 KB, which 36
    periods of tier tables would blow straight through.
    """
    seasons: list[Any] = []
    for segment in getattr(bill, "segments", None) or []:
        season = segment.get("season") if isinstance(segment, dict) else getattr(segment, "season", None)
        if season is not None and season not in seasons:
            seasons.append(season)
    return {
        "days": bill.days,
        "energy_cost": round(bill.energy_cost, 2),
        "surcharge": round(bill.surcharge, 2),
        "fixed_fees": round(bill.fixed_fees, 2),
        "tax": round(bill.tax, 2),
        "estimated_total": round(bill.total, 2),
        "applied_minimum": bool(getattr(bill, "applied_minimum", False)),
        "avg_price_per_kwh": round(bill.avg_price_per_kwh, 4) if bill.avg_price_per_kwh else None,
        "marginal_price_per_kwh": bill.marginal_price_per_kwh,
        "seasons": seasons,
    }


def _reading_brief(record: dict | None) -> dict[str, Any] | None:
    """One ledger row, slim enough for the sensor attribute payload.

    ``computed`` is deliberately left out: it is already summarised in
    ``history``, and repeating it per read date would multiply the attribute
    payload by the number of recorded read dates.
    """
    if not isinstance(record, dict):
        return None
    return {
        "date": record.get("date"),
        "kwh": record.get("kwh"),
        "bill": record.get("bill"),
        "forced": bool(record.get("forced")),
        "note": record.get("note"),
    }


def _reading_payload(record: dict | None) -> dict[str, Any] | None:
    """A copy of one ledger row (never the live object)."""
    if not isinstance(record, dict):
        return None
    return {
        "date": record.get("date"),
        "kwh": record.get("kwh"),
        "bill": record.get("bill"),
        "forced": bool(record.get("forced")),
        "note": record.get("note"),
        "recorded_at": record.get("recorded_at"),
        "computed": record.get("computed"),
    }
