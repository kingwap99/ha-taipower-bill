"""Tests for the Taipower billing engine.

Run:  python3 -I tests/test_billing.py
"""

import importlib.util
import sys
import types
from datetime import date, timedelta
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "taipower_bill"

# Load the two pure-Python modules without executing the package __init__.py,
# which imports Home Assistant (not installed in a standalone test sandbox).
pkg = types.ModuleType("taipower_bill")
pkg.__path__ = [str(PKG)]
sys.modules["taipower_bill"] = pkg


def _load(name):
    spec = importlib.util.spec_from_file_location(f"taipower_bill.{name}", PKG / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_tariffs = _load("tariffs")
_billing = _load("billing")
_validators = _load("validators")

validate_basics = _validators.validate_basics
parse_date = _validators.parse_date

REVISIONS = _tariffs.REVISIONS
is_summer = _tariffs.is_summer
revision_for = _tariffs.revision_for
BillingConfig = _billing.BillingConfig
compute_bill = _billing.compute_bill
split_segments = _billing.split_segments

CURRENT = [r for r in REVISIONS if r.key == "2025-10"][0]
OLD = [r for r in REVISIONS if r.key == "2022-07"][0]
CFG = BillingConfig()

failures = []


def check(label, got, want, tol=0.51):
    ok = abs(got - want) <= tol
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got {got} want ~{want}")
    if not ok:
        failures.append(label)


# --- 1. season window -------------------------------------------------------
assert is_summer(date(2026, 5, 31)) is False
assert is_summer(date(2026, 6, 1)) is True
assert is_summer(date(2026, 9, 30)) is True
assert is_summer(date(2026, 10, 1)) is False
print("PASS  season window: 6/1-9/30 inclusive")

# --- 2. revision lookup ----------------------------------------------------
assert revision_for(date(2026, 10, 1)).key == "2025-10"
assert revision_for(date(2025, 9, 30)).key == "2022-07"
print("PASS  revision lookup straddles 2025-10-01")

# --- 3. the worked example from the rate page ----------------------------
# 9/13 -> 11/11, 60 days, 350 kWh, all under the current table.
cfg2026 = BillingConfig(revisions=(CURRENT,))
r = compute_bill(350, date(2026, 9, 13), date(2026, 11, 11), cfg2026)
assert r.days == 60, r.days
segs = split_segments(date(2026, 9, 13), date(2026, 11, 11), cfg2026)
assert [(s.days, s.summer) for s in segs] == [(18, True), (42, False)], segs
print("PASS  segment split: 18 summer / 42 off")
check("cross-season bill 350kWh", r.total, 685.0)

# --- 4. pure summer, single month ladder doubled -------------------------
# 240 kWh in a bimonthly summer period = exactly tier 1 -> 240 * 1.78
r = compute_bill(240, date(2026, 7, 1), date(2026, 8, 30), cfg2026)
check("240kWh summer tier1 only", r.total, 240 * 1.78, tol=0.01)

# --- 5. top tier ----------------------------------------------------------
r = compute_bill(2500, date(2026, 8, 1), date(2026, 9, 29), cfg2026)
expect = (
    240 * 1.78 + (660 - 240) * 2.55 + (1000 - 660) * 3.80
    + (1400 - 1000) * 5.14 + (2000 - 1400) * 6.44 + 500 * 8.86
)
check("2500kWh summer full ladder", r.total, expect, tol=0.01)
assert r.marginal_price_per_kwh == 8.86, r.marginal_price_per_kwh
print("PASS  marginal price at top tier = 8.86")

# --- 6. minimum charge ----------------------------------------------------
r = compute_bill(20, date(2026, 11, 1), date(2026, 12, 30), cfg2026)
check("tiny usage hits 2x100 minimum", r.total, 200.0, tol=0.01)
assert r.applied_minimum is True
print("PASS  minimum charge applied")

# --- 7. a real rate change inside the period differs from ignoring it --
# 9/13-11/11/2025 straddles the 2025-10-01 revision: Sep is old rates.
r = compute_bill(350, date(2025, 9, 13), date(2025, 11, 11), CFG)
segs = split_segments(date(2025, 9, 13), date(2025, 11, 11), CFG)
assert [(s.days, s.summer, s.revision) for s in segs] == [
    (18, True, "2022-07"),
    (42, False, "2025-10"),
], segs
check("2025 straddle uses both revisions", r.total, 674.87, tol=0.5)
print("PASS  revision change handled independently of season change")

# --- 8. surcharges and tax compose correctly ----------------------------
cfg = BillingConfig(
    surcharge_per_kwh=0.6, fixed_fees_per_period=50.0, business_tax_rate=0.05
)
r = compute_bill(500, date(2026, 1, 5), date(2026, 3, 5), cfg)
energy = 240 * 1.78 + 260 * 2.26
expect = (energy + 500 * 0.6 + 50.0) * 1.05
check("surcharges + tax", r.total, expect, tol=0.01)
assert abs(r.surcharge - 300.0) < 0.01, r.surcharge
print("PASS  surcharge/tax composition")

# --- 9. cnstudio comparison: shows why its numbers drift ----------------
cn = compute_bill(350, date(2026, 7, 1), date(2026, 8, 30), BillingConfig(revisions=(OLD,)))
now = compute_bill(350, date(2026, 7, 1), date(2026, 8, 30), cfg2026)
print(f"INFO  350kWh summer: old table {cn.total:.0f} vs current {now.total:.0f} (diff {now.total-cn.total:.0f})")
assert now.total > cn.total

# --- 10. 上期抄見日 validation: the "values are almost all empty" regression --
TODAY = date(2026, 10, 14)
GOOD = {
    "label": "B戶",
    "source_entities": ["sensor.house_b_energy_this_month"],
    "source_mode": "statistics",
    "last_read_date": "2026-09-13",
    "cycle_days": 60,
}
assert validate_basics(GOOD, TODAY) == {}, validate_basics(GOOD, TODAY)
assert validate_basics({**GOOD, "last_read_date": date(2026, 9, 13)}, TODAY) == {}
assert parse_date("2026/09/13") == date(2026, 9, 13)
assert validate_basics({**GOOD, "last_read_date": "2028-04-29"}, TODAY) == {
    "last_read_date": "last_read_in_future"
}
assert validate_basics({**GOOD, "last_read_date": "2026-13-40"}, TODAY) == {
    "last_read_date": "last_read_invalid"
}
assert validate_basics({**GOOD, "last_read_date": "2020-01-01"}, TODAY) == {
    "last_read_date": "last_read_too_old"
}
assert validate_basics({**GOOD, "cycle_days": 10}, TODAY) == {"cycle_days": "cycle_days_too_short"}
assert validate_basics({**GOOD, "label": "   "}, TODAY) == {"label": "label_required"}
assert validate_basics({**GOOD, "source_entities": []}, TODAY) == {
    "source_entities": "source_required"
}
print("PASS  抄見日 validation rejects future / stale / malformed values")

# --- 11. how the broken install reached 2028-04-29 -------------------------
# Every 「結算本期」 press advances 抄見日 by cycle_days (60).  Ten presses from an
# ordinary 2026-09-07 land exactly on the date that was stored in the entry.
assert date(2026, 9, 7) + timedelta(days=10 * 60) == date(2028, 4, 29)
print("PASS  10 button presses x 60 days: 2026-09-07 -> 2028-04-29 (the broken entry)")

# --- 12. the symptom: an inverted window gives a zero-day, zero-cost bill ---
r = compute_bill(0.084, date(2028, 4, 30), date(2026, 10, 14), cfg2026)
assert r.days == 0, r.days
assert r.total == 0.0, r.total  # the 200 TWD minimum does not even apply
assert r.segments == [] and r.tiers == []
assert r.avg_price_per_kwh is None and r.marginal_price_per_kwh is None
assert (min(date(2026, 10, 14), date(2028, 6, 28)) - date(2028, 4, 30)).days + 1 < 0  # days_elapsed clamps to 0
print("PASS  inverted period window -> 0 days / 0 TWD / no tiers (why every sensor read empty)")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL TESTS PASSED")
