"""Taipower billing engine -- pure Python, no Home Assistant imports.

Kept HA-free on purpose so the money math can be unit-tested standalone
(see tests/test_billing.py).

The cross-season rule is the part most implementations get wrong.  Taipower
does NOT split the tier ladder at the season boundary.  For a period that
straddles 9/30 it computes a **full** progressive bill for each season on the
**total** period kWh, then weights those two bills by that season's share of
the days.  Verified against a worked example (350 kWh over 60 days, 18 summer
days / 42 non-summer days -> ~685 TWD under the 2025-10 table).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .tariffs import (
    MIN_CHARGE_PER_MONTH,
    REVISIONS,
    SUMMER_END,
    SUMMER_START,
    TariffRevision,
    is_summer,
    revision_for,
)


@dataclass(frozen=True)
class BillingConfig:
    """Per-meter configuration, seeded from tariffs.py and user-editable."""

    period_months: int = 2
    cycle_days: int = 60
    min_charge_per_month: float = MIN_CHARGE_PER_MONTH
    summer_start: tuple[int, int] = SUMMER_START
    summer_end: tuple[int, int] = SUMMER_END
    #: Extra per-kWh adders printed on the bill (再生能源球金 + 空污費 ...).
    surcharge_per_kwh: float = 0.0
    #: Extra fixed fee per period (public facility apportionment, ...).
    fixed_fees_per_period: float = 0.0
    #: 營業稅 rate applied to the taxable subtotal.  0.0 = none configured.
    business_tax_rate: float = 0.0
    revisions: tuple[TariffRevision, ...] = REVISIONS


@dataclass
class Segment:
    """Contiguous run of days sharing one season and one tariff revision."""

    start: date
    end: date
    summer: bool
    revision: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


@dataclass
class BillResult:
    """Everything a sensor or card needs to describe one period."""

    start: date
    end: date
    days: int
    kwh: float
    energy_cost: float = 0.0
    surcharge: float = 0.0
    fixed_fees: float = 0.0
    tax: float = 0.0
    total: float = 0.0
    applied_minimum: bool = False
    avg_price_per_kwh: float | None = None
    marginal_price_per_kwh: float | None = None
    segments: list[dict] = field(default_factory=list)
    tiers: list[dict] = field(default_factory=list)

    def as_attributes(self) -> dict:
        """Flatten for sensor attributes.

        NOTE: the window keys are deliberately named ``bill_window_*`` and not
        ``period_start``/``period_end``.  This result covers only the *elapsed*
        part of the period (start -> min(today, end)), while the sensor already
        publishes the full configured window under ``period_start``/
        ``period_end``.  Merging both under the same names used to silently
        overwrite the sensor's own values, which made an inverted period look
        like a valid one on the device page.
        """
        return {
            "bill_window_start": self.start.isoformat(),
            "bill_window_end": self.end.isoformat(),
            "bill_window_days": self.days,
            "kwh": round(self.kwh, 3),
            "energy_cost": round(self.energy_cost, 2),
            "surcharge": round(self.surcharge, 2),
            "fixed_fees": round(self.fixed_fees, 2),
            "tax": round(self.tax, 2),
            "total": round(self.total, 2),
            "applied_minimum": self.applied_minimum,
            "avg_price_per_kwh": round(self.avg_price_per_kwh, 4) if self.avg_price_per_kwh else None,
            "marginal_price_per_kwh": self.marginal_price_per_kwh,
            "segments": self.segments,
            "tiers": self.tiers,
        }


def period_days(start: date, end: date) -> list[date]:
    """Inclusive list of days from start to end."""
    if end < start:
        return []
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def split_segments(start: date, end: date, config: BillingConfig) -> list[Segment]:
    """Split [start, end] into runs of constant (season, tariff revision)."""
    days = period_days(start, end)
    if not days:
        return []
    segments: list[Segment] = []
    cur: Segment | None = None
    for day in days:
        summer = is_summer(day, config.summer_start, config.summer_end)
        rev = revision_for(day, config.revisions)
        if cur and cur.summer == summer and cur.revision == rev.key:
            cur.end = day
        else:
            if cur:
                segments.append(cur)
            cur = Segment(start=day, end=day, summer=summer, revision=rev.key)
    if cur:
        segments.append(cur)
    return segments


def progressive(kwh: float, prices: tuple[float, ...], bounds: tuple[int, ...]) -> tuple[float, list[dict]]:
    """Classic marginal-tier accumulation.

    ``bounds`` are the period-scaled cumulative upper bounds; ``prices`` has
    one entry per tier (len(bounds) + 1).
    """
    remaining = float(kwh)
    cost = 0.0
    prev = 0
    rows: list[dict] = []
    for i, bound in enumerate(bounds):
        width = bound - prev
        take = max(0.0, min(remaining, float(width)))
        tier_cost = take * prices[i]
        cost += tier_cost
        rows.append(
            {
                "tier": i + 1,
                "up_to_kwh": bound,
                "kwh": round(take, 3),
                "price": prices[i],
                "cost": round(tier_cost, 2),
            }
        )
        remaining -= take
        prev = bound
        if remaining <= 0:
            break
    if remaining > 0:
        top = len(bounds)
        price = prices[top] if top < len(prices) else prices[-1]
        tier_cost = remaining * price
        cost += tier_cost
        rows.append(
            {
                "tier": top + 1,
                "up_to_kwh": None,
                "kwh": round(remaining, 3),
                "price": price,
                "cost": round(tier_cost, 2),
            }
        )
    return cost, rows


def marginal_price(kwh: float, prices: tuple[float, ...], bounds: tuple[int, ...]) -> float:
    """Price the *next* kWh would be billed at -- useful for 'what if'."""
    for i, bound in enumerate(bounds):
        if kwh < bound:
            return prices[i]
    return prices[-1]


def compute_bill(
    kwh: float,
    start: date,
    end: date,
    config: BillingConfig | None = None,
) -> BillResult:
    """Compute one billing period's bill.

    Cross-season / cross-revision periods are handled by weighting a FULL
    progressive bill per season by that season's day share -- see module
    docstring.
    """
    config = config or BillingConfig()
    days = period_days(start, end)
    total_days = len(days)
    result = BillResult(start=start, end=end, days=total_days, kwh=float(kwh))
    if total_days == 0:
        return result

    segments = split_segments(start, end, config)
    energy_cost = 0.0
    for seg in segments:
        rev = next(r for r in config.revisions if r.key == seg.revision)
        prices = rev.prices(seg.summer)
        bounds = rev.bounds(config.period_months)
        full_cost, _rows = progressive(float(kwh), prices, bounds)
        share = seg.days / total_days
        weighted = full_cost * share
        energy_cost += weighted
        result.segments.append(
            {
                "start": seg.start.isoformat(),
                "end": seg.end.isoformat(),
                "days": seg.days,
                "season": "summer" if seg.summer else "off-peak-season",
                "revision": seg.revision,
                "day_share": round(share, 4),
                "full_season_cost": round(full_cost, 2),
                "weighted_cost": round(weighted, 2),
            }
        )

    # Tier ladder snapshot for the dominant season (what a bill prints).
    dominant = max(segments, key=lambda s: s.days) if segments else None
    if dominant:
        rev = next(r for r in config.revisions if r.key == dominant.revision)
        _, result.tiers = progressive(
            float(kwh), rev.prices(dominant.summer), rev.bounds(config.period_months)
        )
        result.marginal_price_per_kwh = marginal_price(
            float(kwh), rev.prices(dominant.summer), rev.bounds(config.period_months)
        )

    surcharge = float(kwh) * config.surcharge_per_kwh
    fixed = config.fixed_fees_per_period
    months = max(1, round(total_days / 30.44))
    minimum = config.min_charge_per_month * months
    subtotal = energy_cost + surcharge + fixed
    taxable = subtotal
    if config.business_tax_rate:
        tax = taxable * config.business_tax_rate
    else:
        tax = 0.0
    total = subtotal + tax
    if total < minimum:
        total = minimum
        result.applied_minimum = True

    result.energy_cost = energy_cost
    result.surcharge = surcharge
    result.fixed_fees = fixed
    result.tax = tax
    result.total = total
    result.avg_price_per_kwh = (total / float(kwh)) if kwh else None
    return result
