"""
Isolation test for strategy/risk_sizing.py (JPY risk-based position sizing).

Two parts
---------
PART A - offline math (no MT5): exercises floor_to_step / lot_for_risk /
         expected_loss_jpy with a synthetic SymbolSizing. Covers normal sizing,
         step flooring, min-skip, and max-clamp edges. Deterministic, no network.

PART B - live round-trip (read-only, NO trades): for each of the 6 live symbols,
         probe the JPY/price/lot constant via order_calc_profit, size a lot for a
         target JPY risk, then feed that lot + stop distance back through
         order_calc_profit and confirm the broker-computed loss ~= target risk.
         This proves our arithmetic agrees with MT5's own P&L engine.

Usage
-----
    .venv\\Scripts\\python tests\\test_risk_sizing.py            (both parts)
    .venv\\Scripts\\python tests\\test_risk_sizing.py --offline  (Part A only)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import MetaTrader5 as mt5  # noqa: E402

from strategy.risk_sizing import (  # noqa: E402
    SymbolSizing,
    expected_loss_jpy,
    floor_to_step,
    lot_for_risk,
    probe_symbol_sizing,
)

_ENV_FILE = _REPO_ROOT / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _v:
                os.environ.setdefault(_k.strip(), _v.strip())

_PASS = 0
_FAIL = 0


def ok(msg: str) -> None:
    global _PASS
    _PASS += 1
    print(f"  [PASS] {msg}")


def bad(msg: str) -> None:
    global _FAIL
    _FAIL += 1
    print(f"  [FAIL] {msg}")


def info(msg: str) -> None:
    print(f"  [info] {msg}")


def step(msg: str) -> None:
    print(f"\n=== {msg} ===")


def approx(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


# --------------------------------------------------------------------------- #
# PART A - offline math
# --------------------------------------------------------------------------- #
def part_a_offline() -> None:
    step("PART A - offline math (no MT5)")

    # floor_to_step
    if approx(floor_to_step(0.137, 0.01), 0.13, 1e-9):
        ok("floor_to_step(0.137, 0.01) == 0.13")
    else:
        bad(f"floor_to_step(0.137, 0.01) == {floor_to_step(0.137, 0.01)}")
    if approx(floor_to_step(0.10, 0.01), 0.10, 1e-9):
        ok("floor_to_step(0.10, 0.01) == 0.10 (on-step value not dropped)")
    else:
        bad(f"floor_to_step(0.10, 0.01) == {floor_to_step(0.10, 0.01)}")

    # GOLD-like spec: 15809 JPY per 1.0 price move per 1.0 lot, step 0.01
    gold = SymbolSizing("GOLD", 15809.0, 0.01, 50.0, 0.01, 2)

    # Target risk 5000 JPY, stop distance 10.0 price units.
    # loss_per_lot = 10 * 15809 = 158090; raw = 5000/158090 = 0.03163 -> floor 0.03
    vol = lot_for_risk(5000.0, 10.0, gold)
    if approx(vol, 0.03, 1e-9):
        ok(f"lot_for_risk(5000 JPY, 10.0 px, GOLD) == 0.03 (got {vol})")
    else:
        bad(f"lot_for_risk(5000 JPY, 10.0 px, GOLD) expected 0.03, got {vol}")

    # realized loss at 0.03 lot should be <= target (because we floored)
    loss = expected_loss_jpy(vol, 10.0, gold)
    if loss <= 5000.0 + 1e-6:
        ok(f"expected_loss_jpy(0.03, 10.0) = {loss:.1f} JPY <= 5000 (floored, not over)")
    else:
        bad(f"expected_loss_jpy(0.03, 10.0) = {loss:.1f} JPY > 5000")

    # min-skip: tiny risk budget that can't afford even volume_min (0.01)
    # loss at 0.01 lot over 10 px = 1580.9; ask for 1000 JPY -> must skip (0.0)
    vol_skip = lot_for_risk(1000.0, 10.0, gold)
    if vol_skip == 0.0:
        ok("lot_for_risk returns 0.0 (skip) when volume_min exceeds the risk budget")
    else:
        bad(f"expected skip (0.0), got {vol_skip}")

    # max-clamp: huge risk budget clamps to volume_max
    vol_max = lot_for_risk(10_000_000.0, 10.0, gold)
    if approx(vol_max, gold.volume_max, 1e-9):
        ok(f"lot_for_risk clamps to volume_max ({gold.volume_max}) on a huge budget")
    else:
        bad(f"expected clamp to {gold.volume_max}, got {vol_max}")

    # guards: non-positive inputs return 0.0
    if lot_for_risk(0.0, 10.0, gold) == 0.0 and lot_for_risk(5000.0, 0.0, gold) == 0.0:
        ok("lot_for_risk guards non-positive risk/stop -> 0.0")
    else:
        bad("lot_for_risk did not guard non-positive inputs")


# --------------------------------------------------------------------------- #
# PART B - live round-trip against MT5 (read-only, no trades)
# --------------------------------------------------------------------------- #
def part_b_live() -> int:
    step("PART B - live round-trip vs order_calc_profit (read-only)")
    kw: dict = {}
    if os.environ.get("MT5_TERMINAL_PATH"):
        kw["path"] = os.environ["MT5_TERMINAL_PATH"]
    if os.environ.get("MT5_LOGIN"):
        kw["login"] = int(os.environ["MT5_LOGIN"])
    if os.environ.get("MT5_PASSWORD"):
        kw["password"] = os.environ["MT5_PASSWORD"]
    if os.environ.get("MT5_SERVER"):
        kw["server"] = os.environ["MT5_SERVER"]
    if not mt5.initialize(**kw):
        bad(f"mt5.initialize() failed: {mt5.last_error()}")
        return 1

    try:
        acc = mt5.account_info()
        ok(f"connected: {acc.currency} balance={acc.balance}")
        target_risk = 5000.0  # JPY per trade for the test
        symbols = ["GOLD", "#USNDAQ100", "#USSPX500", "WTI", "BITCOIN", "#Japan225"]

        for sym in symbols:
            spec = probe_symbol_sizing(sym)
            if spec is None:
                bad(f"{sym}: probe_symbol_sizing returned None (unavailable?)")
                continue

            tick = mt5.symbol_info_tick(sym)
            price = tick.ask
            info_s = mt5.symbol_info(sym)
            # stop distance = 1% of price (arbitrary but realistic for the check)
            stop_dist = round(price * 0.01, info_s.digits)

            vol = lot_for_risk(target_risk, stop_dist, spec)
            if vol == 0.0:
                info(f"{sym}: lot_for_risk -> skip (min lot risks > {target_risk:.0f} JPY "
                     f"at {stop_dist} stop); our loss@min="
                     f"{expected_loss_jpy(spec.volume_min, stop_dist, spec):.0f}")
                # verify the skip is correct: min-lot loss really does exceed target
                if expected_loss_jpy(spec.volume_min, stop_dist, spec) > target_risk:
                    ok(f"{sym}: skip is correct (min-lot loss exceeds budget)")
                else:
                    bad(f"{sym}: skipped but min-lot loss within budget")
                continue

            # Our predicted JPY loss for that lot at the stop:
            our_loss = expected_loss_jpy(vol, stop_dist, spec)
            # MT5's own loss for a BUY stopped out stop_dist below entry:
            mt5_loss = -mt5.order_calc_profit(
                mt5.ORDER_TYPE_BUY, sym, vol, price, price - stop_dist
            )
            # They must agree to within rounding (allow 0.5% or 1 JPY)
            tol = max(1.0, 0.005 * abs(mt5_loss))
            if approx(our_loss, mt5_loss, tol):
                ok(f"{sym}: lot={vol} stop={stop_dist}  our_loss={our_loss:.0f} "
                   f"~= mt5_loss={mt5_loss:.0f} JPY (<= target {target_risk:.0f})")
            else:
                bad(f"{sym}: our_loss={our_loss:.1f} vs mt5_loss={mt5_loss:.1f} "
                    f"(tol {tol:.1f})")
            # And the realized risk should not exceed the target (we floored)
            if mt5_loss > target_risk + tol:
                bad(f"{sym}: realized risk {mt5_loss:.0f} exceeds target {target_risk:.0f}")

        return 0
    finally:
        mt5.shutdown()


def main() -> int:
    ap = argparse.ArgumentParser(description="risk_sizing isolation test")
    ap.add_argument("--offline", action="store_true", help="run Part A only (no MT5)")
    args = ap.parse_args()

    print("=" * 64)
    print(" risk_sizing isolation test")
    print("=" * 64)

    part_a_offline()
    rc = 0
    if not args.offline:
        rc = part_b_live()

    print("\n" + "=" * 64)
    print(f" RESULT: {_PASS} passed, {_FAIL} failed  ->  "
          f"{'OK' if (_FAIL == 0 and rc == 0) else 'PROBLEMS'}")
    print("=" * 64)
    return 1 if (_FAIL > 0 or rc != 0) else 0


if __name__ == "__main__":
    sys.exit(main())
