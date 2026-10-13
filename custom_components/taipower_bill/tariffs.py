"""Versioned Taipower (台電) tariff tables for 表燈 非時間電價.

Sources / provenance
-------------------
Tier *thresholds* are published per **month**.  Taipower reads a 表燈 meter
roughly every 60 days, so a billing period's thresholds are the monthly
thresholds scaled by the number of months the period covers (2 for a normal
60-day period).  This is confirmed by cnstudio's shipped implementation,
which uses the doubled bounds 240/660/1000/1400/2000.

Prices below are 流動電費 only.  They deliberately do NOT include
再生能源發展球金, 空污費, 營業稅 or 基本電費: those live in user-editable
config fields (``surcharge_per_kwh`` / ``fixed_fees_per_period`` /
``business_tax_rate``) because the exact adders are printed on each bill and
change by year.  See README for how to read them off a real bill.

Revision ``2025-10`` is the table stated to be effective 2025-10-01.
Revision ``2022-07`` is the older table cnstudio shipped; its effective date
is approximate and flagged ``confirmed=False`` -- correct it from an old bill
if you care about historical periods that straddle it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class TariffRevision:
    """One published revision of the 表燈非時間電價 table."""

    key: str
    effective_from: date
    #: Monthly cumulative upper bounds, ascending.  Tiers = len(bounds) + 1.
    monthly_bounds: tuple[int, ...]
    #: Price per kWh for each tier, summer (夏月).
    summer_prices: tuple[float, ...]
    #: Price per kWh for each tier, non-summer (非夏月).
    off_prices: tuple[float, ...]
    #: False when the effective date is an approximation.
    confirmed: bool = True
    note: str = ""

    def prices(self, summer: bool) -> tuple[float, ...]:
        return self.summer_prices if summer else self.off_prices

    def bounds(self, period_months: int) -> tuple[int, ...]:
        """Bounds scaled to the billing period (2 for a 60-day bill)."""
        return tuple(b * period_months for b in self.monthly_bounds)


#: Monthly cumulative upper bounds of the six progressive tiers.
MONTHLY_BOUNDS: tuple[int, ...] = (120, 330, 500, 700, 1000)

REVISIONS: tuple[TariffRevision, ...] = (
    TariffRevision(
        key="2022-07",
        effective_from=date(2022, 7, 1),
        monthly_bounds=MONTHLY_BOUNDS,
        summer_prices=(1.68, 2.45, 3.70, 5.04, 6.24, 8.46),
        off_prices=(1.68, 2.16, 3.03, 4.14, 5.07, 6.63),
        confirmed=False,
        note="cnstudio 舊表；生效日為近似值，請依實際電費單校正",
    ),
    TariffRevision(
        key="2025-10",
        effective_from=date(2025, 10, 1),
        monthly_bounds=MONTHLY_BOUNDS,
        summer_prices=(1.78, 2.55, 3.80, 5.14, 6.44, 8.86),
        off_prices=(1.78, 2.26, 3.13, 4.24, 5.27, 7.03),
        confirmed=True,
        note="表燈非時間電價，生效 2025-10-01",
    ),
)

#: 夏月 = 6/1 .. 9/30 (inclusive), as (start_month_day, end_month_day).
SUMMER_START = (6, 1)
SUMMER_END = (9, 30)

#: 表燈 non-TOU has no 契約容量 basic fee; instead a monthly minimum.
MIN_CHARGE_PER_MONTH = 100.0


def revision_for(day: date, revisions: tuple[TariffRevision, ...] = REVISIONS) -> TariffRevision:
    """Return the revision in force on ``day``."""
    chosen = None
    for rev in sorted(revisions, key=lambda r: r.effective_from):
        if day >= rev.effective_from:
            chosen = rev
    if chosen is None:  # day predates every known revision
        chosen = min(revisions, key=lambda r: r.effective_from)
    return chosen


def is_summer(day: date, summer_start=SUMMER_START, summer_end=SUMMER_END) -> bool:
    """True when ``day`` falls inside the summer rate window.

    Supports a window that wraps the new year (start > end).
    """
    md = (day.month, day.day)
    if summer_start <= summer_end:
        return summer_start <= md <= summer_end
    return md >= summer_start or md <= summer_end
