"""
Judge the short detectors (S1 / S2 / S4) head-to-head on GOLD H1.

Runs each detector SEPARATELY (clean per-detector attribution), then the
combined set, over a chosen window with the GOLD multiplier=100 fix so PnL is
real. Also cross-checks that MultiShortEMA(S1) reproduces the proven
ShortEMACross control.

Each run gets the same bug checks as run_backtest_short.py.

Run:
    .venv\\Scripts\\python run_backtest_multi.py --start 2023-01-01 --end 2026-08-20
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.model.currencies import JPY, USD
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.risk.config import RiskEngineConfig

from adapters.mt5.instrument import build_gold_instrument
from strategy.multi_short import MultiShortEMA, MultiShortConfig
from strategy.short_ema_cross import ShortEMACross, ShortEMACrossConfig

BAR_TYPE_STR = "GOLD.FXPRO-1-HOUR-LAST-EXTERNAL"
CATALOG_PATH = Path(__file__).parent / "catalog"
VENUE = Venue("FXPRO")


def _ns_to_dt(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _to_ns(date_str: str) -> int:
    return int(pd.Timestamp(date_str, tz="UTC").value)


def _num(x) -> float:
    try:
        return float(str(x).split()[0])
    except (ValueError, IndexError):
        return 0.0


def _new_engine() -> BacktestEngine:
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            logging=LoggingConfig(log_level="ERROR"),
            risk_engine=RiskEngineConfig(bypass=True),
        )
    )
    engine.add_venue(
        venue=VENUE,
        oms_type=OmsType.HEDGING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(1_000_000, JPY), Money(10_000, USD)],
        base_currency=None,
    )
    engine.add_instrument(build_gold_instrument(multiplier=100))
    return engine


def _stats(engine, label: str) -> dict:
    fills = engine.trader.generate_fills_report()
    positions = engine.trader.generate_positions_report()
    n_sell = n_buy = n_closed = wins = 0
    pnl_sum = 0.0
    sign_mismatch = None
    if fills is not None and not fills.empty:
        n_sell = int((fills["order_side"] == "SELL").sum())
        n_buy = int((fills["order_side"] == "BUY").sum())
    if positions is not None and not positions.empty:
        n_closed = len(positions)
        pnls = positions["realized_pnl"].map(_num) if "realized_pnl" in positions.columns else pd.Series(dtype=float)
        pnl_sum = float(pnls.sum())
        wins = int((pnls > 0).sum())
        ec = next((c for c in ("avg_px_open", "avg_px_entry") if c in positions.columns), None)
        xc = next((c for c in ("avg_px_close", "avg_px_exit") if c in positions.columns), None)
        if ec and xc:
            e = positions[ec].map(_num); x = positions[xc].map(_num)
            nz = pnls.abs() >= 1e-9
            sign_mismatch = int((((pnls > 0) != (x < e)) & nz).sum())
    open_left = len(engine.cache.positions_open())
    return {
        "label": label, "sell": n_sell, "buy": n_buy, "closed": n_closed,
        "wins": wins, "losses": n_closed - wins, "pnl": pnl_sum,
        "sign_mismatch": sign_mismatch, "open_left": open_left,
    }


def run_multi(detectors: tuple[str, ...], bars, label: str) -> dict:
    engine = _new_engine()
    engine.add_data(bars)
    cfg = MultiShortConfig(
        instruments=("GOLD.FXPRO",),
        bar_spec="1-HOUR-LAST-EXTERNAL",
        fast_ema_period=20, slow_ema_period=50, atr_period=14,
        trade_sizes={"GOLD.FXPRO": "0.01"},
        warmup_bars=200, sl_atr_mult=1.5, tp_atr_mult=3.0,
        breakeven_r=1.0, trail_sl=True,
        detectors=detectors, pullback_atr=0.25, donchian_period=20,
    )
    engine.add_strategy(MultiShortEMA(config=cfg))
    engine.run()
    s = _stats(engine, label)
    engine.dispose()
    return s


def run_control(bars) -> dict:
    """ShortEMACross (the proven S1 control) for cross-check."""
    engine = _new_engine()
    engine.add_data(bars)
    cfg = ShortEMACrossConfig(
        instruments=("GOLD.FXPRO",),
        bar_spec="1-HOUR-LAST-EXTERNAL",
        fast_ema_period=20, slow_ema_period=50, atr_period=14,
        trade_sizes={"GOLD.FXPRO": "0.01"},
        warmup_bars=200, sl_atr_mult=1.5, tp_atr_mult=3.0,
        breakeven_r=1.0, trail_sl=True,
    )
    engine.add_strategy(ShortEMACross(config=cfg))
    engine.run()
    s = _stats(engine, "control:ShortEMACross(S1)")
    engine.dispose()
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    catalog = ParquetDataCatalog(str(CATALOG_PATH))
    all_bars = catalog.bars(bar_types=[BAR_TYPE_STR])
    start_ns = _to_ns(args.start)
    end_ns = _to_ns(args.end) if args.end else None
    bars = [b for b in all_bars if b.ts_event >= start_ns and (end_ns is None or b.ts_event <= end_ns)]
    if not bars:
        raise RuntimeError(f"No bars in range {args.start}..{args.end}")

    print(f"Detector shoot-out on GOLD H1  "
          f"({_ns_to_dt(bars[0].ts_event)} to {_ns_to_dt(bars[-1].ts_event)}, {len(bars):,} bars)")
    print("  first ~200 bars are EMA warmup\n")

    runs = [
        run_control(bars),
        run_multi(("S1",), bars, "S1 only"),
        run_multi(("S2",), bars, "S2 only"),
        run_multi(("S4",), bars, "S4 only"),
        run_multi(("S1", "S2", "S4"), bars, "S1+S2+S4"),
    ]

    # ---- comparison table ----
    print("=" * 84)
    hdr = f"{'strategy':<28}{'trades':>7}{'wins':>6}{'loss':>6}{'win%':>7}{'sum_pnl':>12}{'pnl/trade':>11}"
    print(hdr)
    print("-" * 84)
    for s in runs:
        t = s["closed"]
        win_pct = (100.0 * s["wins"] / t) if t else 0.0
        ppt = (s["pnl"] / t) if t else 0.0
        print(f"{s['label']:<28}{t:>7}{s['wins']:>6}{s['losses']:>6}"
              f"{win_pct:>6.1f}%{s['pnl']:>12.2f}{ppt:>11.3f}")
    print("=" * 84)

    # ---- bug checks across all runs ----
    print("\nBUG CHECKS")
    problems = []
    for s in runs:
        bal = s["sell"] == s["buy"]
        orphan = s["closed"] == s["sell"]
        signok = (s["sign_mismatch"] in (0, None))
        noopen = s["open_left"] == 0
        tag = "OK" if (bal and orphan and signok and noopen) else "PROBLEM"
        if tag == "PROBLEM":
            problems.append(s["label"])
        print(f"  {s['label']:<28} balanced={bal} no_orphan={orphan} "
              f"sign_ok={signok} none_open={noopen}  [{tag}]")

    # ---- S1 cross-check ----
    ctrl = runs[0]
    s1 = runs[1]
    match = (ctrl["closed"] == s1["closed"] and abs(ctrl["pnl"] - s1["pnl"]) < 1e-6)
    print("\nCROSS-CHECK: MultiShortEMA(S1) reproduces ShortEMACross control")
    print(f"  control trades={ctrl['closed']} pnl={ctrl['pnl']:.2f} | "
          f"S1 trades={s1['closed']} pnl={s1['pnl']:.2f}  -> "
          f"{'MATCH' if match else 'DIFFER'}")

    print("\n" + "=" * 84)
    if problems or not match:
        print(f"RESULT: PROBLEMS in {problems}" + ("" if match else " + S1 cross-check DIFFERS"))
    else:
        print("RESULT: all runs pass bug checks; S1 cross-check matches control.")
    print("=" * 84)


if __name__ == "__main__":
    main()
