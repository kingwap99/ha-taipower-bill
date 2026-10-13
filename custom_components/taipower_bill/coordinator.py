"""Data coordinator for Taipower Bill.

Resolves, once per poll, how many kWh have been consumed inside the *actual*
meter-read window (上期抄見日 -> today), then runs the billing engine.

Usage is resolved through three paths, best available first:

1. ``statistics`` -- the recorder's daily sums for each configured source
   entity, differenced across the window.  Accurate and back-fills retroactively.
2. ``live delta`` -- current cumulative state of the sources minus a baseline
   captured when the period opened.  Used when the recorder has no rows yet.
3. ``manual`` -- the number you key in from the paper bill.  Always available,
   so bill tracking keeps working even when the meter telemetry is down.

**Guarding against an unusable window.**  Everything here is anchored on
``last_read_date`` (上期抄見日).  If that date is wrong -- most commonly because
「結算本期」 was pressed by accident a few times, each press pushing 抄見日 forward
by ``cycle_days`` -- the whole period window ends up in the future.  The old
behaviour was to quietly compute a zero-day bill against an inverted window
(``start > end``) and publish a cascade of empty/unknown sensors, which looks
exactly like "the integration is broken".  Now an unusable window is detected up
front, no bill is computed, no baseline is written, and ``config_warning``
carries a human-readable explanation that the card surfaces at the top.
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

from .billing import BillingConfig, compute_bill
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
)
from .tariffs import REVISIONS
from .validators import parse_date

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(minutes=5)
MAX_HISTORY = 36

#: Warn when 抄見日 has not moved for this many cycles (missed 結算本期 presses).
STALE_CYCLES = 2


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


class TaipowerLedger:
    """Persisted ledger of closed billing periods for one meter."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = Store(hass, STORAGE_VERSION, f"{STORAGE_PREFIX}.{entry_id}")
        self.data: dict[str, Any] = {"periods": [], "baseline_kwh": None, "baseline_date": None}

    async def async_load(self) -> None:
        saved = await self._store.async_load()
        if isinstance(saved, dict):
            self.data.update(saved)

    async def async_save(self) -> None:
        await self._store.async_save(self.data)

    @property
    def periods(self) -> list[dict]:
        return self.data.get("periods", [])

    def close_period(self, record: dict) -> None:
        periods = self.data.setdefault("periods", [])
        periods = [p for p in periods if p.get("period_start") != record["period_start"]]
        periods.append(record)
        periods.sort(key=lambda p: p.get("period_start") or "")
        self.data["periods"] = periods[-MAX_HISTORY:]

    def forget_period(self, period_start: str) -> bool:
        periods = self.data.get("periods", [])
        kept = [p for p in periods if p.get("period_start") != period_start]
        if len(kept) == len(periods):
            return False
        self.data["periods"] = kept
        return True

    def reset(self, clear_baseline: bool = True) -> int:
        """Drop every recorded period.  Returns how many were removed."""
        dropped = len(self.periods)
        self.data["periods"] = []
        if clear_baseline:
            self.data["baseline_kwh"] = None
            self.data["baseline_date"] = None
        return dropped


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
        #: why the last recorder statistics query failed, if it did.  Surfaced on
        #: the sensors so a silent fall back to the live-delta path is visible.
        self.stats_error: str | None = None

        rev_key = self.options.get(CONF_TARIFF_REVISION)
        revisions = REVISIONS
        if rev_key and rev_key != "latest":
            chosen = tuple(r for r in REVISIONS if r.key == rev_key)
            revisions = chosen or REVISIONS

        self.billing_config = BillingConfig(
            period_months=self.period_months,
            cycle_days=self.cycle_days,
            min_charge_per_month=float(self.options.get(CONF_MIN_CHARGE_PER_MONTH, DEFAULT_MIN_CHARGE_PER_MONTH)),
            summer_start=_parse_month_day(self.options.get(CONF_SUMMER_START), (6, 1)),
            summer_end=_parse_month_day(self.options.get(CONF_SUMMER_END), (9, 30)),
            surcharge_per_kwh=float(self.options.get(CONF_SURCHARGE_PER_KWH, 0.0) or 0.0),
            fixed_fees_per_period=float(self.options.get(CONF_FIXED_FEES_PER_PERIOD, 0.0) or 0.0),
            business_tax_rate=float(self.options.get(CONF_BUSINESS_TAX_RATE, 0.0) or 0.0),
            revisions=revisions,
        )

        super().__init__(hass, _LOGGER, name=f"{DOMAIN} {self.label}", update_interval=POLL_INTERVAL)

    # -- period window -----------------------------------------------------
    @property
    def last_read_date(self) -> date:
        parsed = parse_date(self.options.get(CONF_LAST_READ_DATE))
        return parsed or dt_util.now().date()

    @property
    def period_start(self) -> date:
        """First day *counted* in the current bill = the day after 上期抄見日."""
        return self.last_read_date + timedelta(days=1)

    @property
    def period_end(self) -> date:
        """Projected 本期抄見日."""
        return self.last_read_date + timedelta(days=self.cycle_days)

    @property
    def today(self) -> date:
        return dt_util.now().date()

    @property
    def period_has_started(self) -> bool:
        """False when 上期抄見日 sits in the future (a mis-set or over-advanced date)."""
        return self.period_start <= self.today

    @property
    def period_has_ended(self) -> bool:
        return self.today >= self.period_end

    @property
    def days_elapsed(self) -> int:
        """Days of the window already counted, 0 when the period has not started."""
        if not self.period_has_started:
            return 0
        return (min(self.today, self.period_end) - self.period_start).days + 1

    # -- configuration sanity ---------------------------------------------
    @property
    def config_issues(self) -> list[str]:
        """Machine-readable list of things that make the numbers untrustworthy."""
        issues: list[str] = []
        if not self.period_has_started:
            issues.append("last_read_in_future")
        elif self.today > self.period_end + timedelta(days=self.cycle_days * (STALE_CYCLES - 1)):
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
        if "last_read_in_future" in issues:
            parts.append(
                f"上期抄見日 {self.last_read_date:%Y-%m-%d} 晚於今天 {self.today:%Y-%m-%d}，"
                f"本期尚未開始，所有數值都無法計算。最常見的原因是「結算本期」被多按了幾次"
                f"（每按一次就把抄見日往後推 {self.cycle_days} 天）。"
                f"請到整合的「重新設定」把抄見日改回電費單上的真實日期"
                f"（可同時勾選清除已結算歷史）。"
            )
        if "last_read_stale" in issues:
            parts.append(
                f"上期抄見日 {self.last_read_date:%Y-%m-%d} 距今天 {self.today:%Y-%m-%d} "
                f"已超過 {STALE_CYCLES} 個週期，可能漏了更新抄見日。"
            )
        if "no_source_entities" in issues:
            parts.append("統計模式沒有設定任何 kWh 來源，請在「重新設定」補上。")
        if "cycle_days_suspicious" in issues:
            parts.append(f"抄表間隔只有 {self.cycle_days} 天，雙月帳單通常是 60 天。")
        return " ／ ".join(parts)

    def _log_issues_once(self) -> None:
        for code in self.config_issues:
            if code in self._logged_issues:
                continue
            self._logged_issues.add(code)
            _LOGGER.error(
                "Taipower Bill '%s': 設定有問題 (%s) — 上期抄見日=%s, 今天=%s, 週期=%s 天. %s",
                self.label,
                code,
                self.last_read_date,
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

    async def _statistics_kwh(self, start: date, end: date) -> tuple[float | None, date | None]:
        """Sum kWh across the sources from the recorder's daily statistics.

        Returns ``(kwh, first_day_found)``.  ``first_day_found`` lets the caller
        tell the user when the window is only partly covered (e.g. the source
        entity was created after 上期抄見日).  ``stats_last_day`` is set as a side
        effect so the sensors can show how fresh the figure is -- today's row is
        still partial and grows until midnight.

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
        self.stats_last_day = None
        if end < start or not self.source_entities:
            return None, None

        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import (
                statistics_during_period,
            )
        except ImportError as err:  # recorder unavailable/disabled
            self._note_stats_error(f"recorder 無法載入 ({err})")
            return None, None

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
            self._note_stats_error(f"{type(err).__name__}: {err}")
            return None, None

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
            self._note_stats_error("統計查詢沒有回傳任何資料列（來源可能還沒有長期統計）")
            return None, None

        self.stats_last_day = latest
        self._note_stats_error(None)
        return total, earliest

    def _note_stats_error(self, message: str | None) -> None:
        """Remember why the statistics path failed, logging only on a change.

        The previous version swallowed every failure in a ``debug`` line nobody
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

        stats, coverage = await self._statistics_kwh(self.period_start, self.today)
        self.stats_coverage_start = coverage
        if stats is not None and stats >= 0:
            return (stats, "statistics")

        live = self._live_sum()
        if live is None:
            return (None, "none")

        baseline = self.ledger.data.get("baseline_kwh")
        if baseline is None:
            self.ledger.data["baseline_kwh"] = live
            self.ledger.data["baseline_date"] = self.today.isoformat()
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
            await self.ledger.async_save()
            return (0.0, "live-delta-reset")
        return (delta, "live-delta")

    # -- polling -----------------------------------------------------------
    async def _async_update_data(self) -> dict[str, Any]:
        self._log_issues_once()
        warning = self._config_warning()
        today = self.today
        start, end = self.period_start, self.period_end

        base: dict[str, Any] = {
            "label": self.label,
            "period_start": start,
            "period_end": end,
            "cycle_days": self.cycle_days,
            "next_read_date": end,
            "days_remaining": None,
            "days_elapsed": 0,
            "kwh": None,
            "cost": None,
            "avg_daily_kwh": None,
            "projected_kwh": None,
            "projected_cost": None,
            "avg_price_per_kwh": None,
            "marginal_price_per_kwh": None,
            "actual_bill": self.actual_bill,
            "bill_variance": None,
            "bill": None,
            "usage_source": "inactive",
            "config_issues": self.config_issues,
            "config_warning": warning,
            "stats_coverage_start": None,
            "stats_last_day": None,
            "stats_error": self.stats_error,
            "history": self.ledger.periods,
        }

        if not self.period_has_started:
            # Nothing to resolve, and deliberately no baseline write: the window
            # itself is meaningless until 抄見日 is fixed.
            base["usage_source"] = "period-not-started"
            return base

        kwh, path = await self._resolve_kwh()
        window_end = min(today, end)
        bill = compute_bill(kwh if kwh is not None else 0.0, start, window_end, self.billing_config)

        days_elapsed = self.days_elapsed
        avg_daily = (kwh / days_elapsed) if (kwh is not None and days_elapsed > 0) else None
        projected_kwh = avg_daily * self.cycle_days if avg_daily is not None else None
        projected_bill = (
            compute_bill(projected_kwh, start, end, self.billing_config)
            if projected_kwh is not None
            else None
        )

        base.update(
            {
                "bill": bill,
                "kwh": kwh,
                "usage_source": path,
                "cost": bill.total if kwh is not None else None,
                "avg_daily_kwh": avg_daily,
                "projected_kwh": projected_kwh,
                "projected_cost": projected_bill.total if projected_bill else None,
                "avg_price_per_kwh": bill.avg_price_per_kwh if kwh else None,
                "marginal_price_per_kwh": bill.marginal_price_per_kwh if kwh is not None else None,
                "days_remaining": max(0, (end - today).days),
                "days_elapsed": days_elapsed,
                "bill_variance": (
                    bill.total - self.actual_bill
                    if (kwh is not None and self.actual_bill is not None)
                    else None
                ),
                "stats_coverage_start": self.stats_coverage_start,
                "stats_last_day": self.stats_last_day,
                "stats_error": self.stats_error,
            }
        )

        if path == "none":
            _LOGGER.debug("%s: no usage data; sources unavailable", self.label)
        return base

    # -- period lifecycle --------------------------------------------------
    async def _persist_options(self) -> None:
        new_data = {**self.entry.data, **self.options}
        try:
            self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("%s: could not persist entry data: %s", self.label, err)

    def _assert_closeable(self, force: bool) -> None:
        if self.period_start > self.period_end:
            raise HomeAssistantError(
                f"{self.label}: 抄見日設定異常（本期起算日 {self.period_start} 晚於抄見日 "
                f"{self.period_end}），請先修正設定。"
            )
        if force:
            return
        if not self.period_has_started:
            raise HomeAssistantError(
                f"{self.label}: 上期抄見日 {self.last_read_date} 晚於今天 {self.today}，"
                f"本期尚未開始，不能結算。請用 taipower_bill.set_last_read_date 服務或"
                f"整合的「重新設定」把抄見日改成電費單上的真實日期。"
            )
        if not self.period_has_ended:
            raise HomeAssistantError(
                f"{self.label}: 本期推定抄見日是 {self.period_end}，今天才 {self.today}，"
                f"還沒到期。若確實要提前結算，請改用服務 taipower_bill.close_period "
                f"並帶 force: true。"
            )

    async def async_close_period(
        self,
        actual_kwh: float | None = None,
        actual_bill: float | None = None,
        force: bool = False,
    ) -> dict:
        """Freeze the current period into history and open the next one.

        Refuses by default unless the period has actually reached its projected
        抄見日, so a mis-click (or a stuck automation) cannot walk 抄見日 off into
        the future one cycle at a time.
        """
        self._assert_closeable(force)

        kwh = actual_kwh if actual_kwh is not None else self.data.get("kwh") if self.data else None
        if kwh is None:
            raise HomeAssistantError(
                f"{self.label}: 沒有可用的本期度數（用量來源目前無資料），"
                f"請先在手動模式填入度數，或改用實際抄見度數呼叫服務。"
            )

        bill = compute_bill(kwh, self.period_start, self.period_end, self.billing_config)
        record = {
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "days": bill.days,
            "kwh": round(float(kwh), 1),
            "energy_cost": round(bill.energy_cost, 2),
            "surcharge": round(bill.surcharge, 2),
            "fixed_fees": round(bill.fixed_fees, 2),
            "tax": round(bill.tax, 2),
            "estimated_total": round(bill.total, 2),
            "actual_total": round(float(actual_bill), 2) if actual_bill is not None else None,
            "variance": (
                round(bill.total - float(actual_bill), 2) if actual_bill is not None else None
            ),
            "avg_price_per_kwh": round(bill.avg_price_per_kwh, 4) if bill.avg_price_per_kwh else None,
            "seasons": [s["season"] for s in bill.segments],
            "source": (self.data or {}).get("usage_source"),
            "forced": bool(force),
        }
        self.ledger.close_period(record)

        new_last_read = self.period_end
        self.options[CONF_LAST_READ_DATE] = new_last_read.isoformat()
        self.ledger.data["baseline_kwh"] = None
        self.ledger.data["baseline_date"] = None
        await self.ledger.async_save()
        await self._persist_options()

        await self.async_request_refresh()
        return record

    async def async_set_last_read_date(
        self, new_date: date, reset_history: bool = False
    ) -> dict:
        """Correct 上期抄見日 (and optionally wipe the bogus history)."""
        if new_date > self.today:
            raise HomeAssistantError(
                f"上期抄見日不能是未來日期（收到 {new_date}，今天是 {self.today}）。"
                f"請填電費單上的「上期抄見日期」，那是已經過去的日期。"
            )
        self.options[CONF_LAST_READ_DATE] = new_date.isoformat()
        self.ledger.data["baseline_kwh"] = None
        self.ledger.data["baseline_date"] = None
        dropped = self.ledger.reset(clear_baseline=True) if reset_history else 0
        self._logged_issues.clear()
        await self.ledger.async_save()
        await self._persist_options()
        await self.async_request_refresh()
        _LOGGER.info(
            "%s: 上期抄見日 corrected to %s (cleared %d history rows, new period %s -> %s)",
            self.label,
            new_date,
            dropped,
            self.period_start,
            self.period_end,
        )
        return {
            "last_read_date": new_date.isoformat(),
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "history_cleared": dropped,
        }

    async def async_reset_ledger(self, clear_baseline: bool = True) -> dict:
        """Drop the recorded history (all of it, or keep the baseline)."""
        dropped = self.ledger.reset(clear_baseline=clear_baseline)
        await self.ledger.async_save()
        await self.async_request_refresh()
        _LOGGER.info("%s: ledger reset, %d period rows removed", self.label, dropped)
        return {"periods_cleared": dropped}
