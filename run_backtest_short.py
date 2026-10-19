"""
Backtest the SHORT EMA crossover strategy (S1 only) on GOLD H1 from the catalog.

Verifies the short strategy's mechanics (death-cross SELL to open, golden-cross /
SL / TP close via reduce-only BUY) on real history, with an explicit bug-check
pass over the fills and closed positions. Reuses the GOLD H1 catalog.

Run:
    .venv\\Scripts\\python run_backtest_short.py                       # 2023-01-01 .. latest
    .venv\\Scripts\\python run_backtest_short.py --start 2023-01-01 --end 2026-08-20
    .venv\\Scripts\\python run_backtest_short.py --start 2008-01-01   # full history
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
from strategy.short_ema_cross import ShortEMACross, ShortEMACrossConfig

BAR_TYPE_STR = "GOLD.FXPRO-1-HOUR-LAST-EXTERNAL"
CATALOG_PATH = Path(__file__).parent / "catalog"
VENUE = Venue("FXPRO")


def _ns_to_dt(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _to_ns(date_str: str) -> int:
    return int(pd.Timestamp(date_str, tz="UTC").value)


# A problem counter so the final line can say OK / PROBLEMS.
_PROBLEMS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  [PASS] {msg}")
    else:
        print(f"  [BUG]  {msg}")
        _PROBLEMS.append(msg)


def main() -> None:
    ap = argparse.ArgumentParser(description="Short strategy backtest + bug checks")
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    catalog = ParquetDataCatalog(str(CATALOG_PATH))
    bars = catalog.bars(bar_types=[BAR_TYPE_STR])
    if not bars:
        raise RuntimeError("No bars in catalog - run pull_h1.py then build_catalog.py first")

    start_ns = _to_ns(args.start)
    end_ns = _to_ns(args.end) if args.end else None
    full = len(bars)
    bars = [b for b in bars if b.ts_event >= start_ns and (end_ns is None or b.ts_event <= end_ns)]
    if not bars:
        raise RuntimeError(f"No bars in range {args.start}..{args.end}")

    print(
        f"SHORT backtest -- {len(bars):,}/{full:,} H1 bars in range"
        f" ({_ns_to_dt(bars[0].ts_event)} to {_ns_to_dt(bars[-1].ts_event)})"
    )
    print("  NOTE: the first ~200 bars are EMA warmup; live trading starts after that.")

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            logging=LoggingConfig(log_level="ERROR"),  # quiet; we print our own summary
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
    # multiplier=100: FxPro GOLD is 100 oz/lot, so Nautilus PnL/notional is correct.
    engine.add_instrument(build_gold_instrument(multiplier=100))
    engine.add_data(bars)

    config = ShortEMACrossConfig(
        instruments=("GOLD.FXPRO",),
        bar_spec="1-HOUR-LAST-EXTERNAL",
        fast_ema_period=20,
        slow_ema_period=50,
        atr_period=14,
        trade_sizes={"GOLD.FXPRO": "0.01"},
        warmup_bars=200,
        sl_atr_mult=1.5,
        tp_atr_mult=3.0,
        breakeven_r=1.0,
        trail_sl=True,
    )
    engine.add_strategy(ShortEMACross(config=config))
    engine.run()

    print("\n" + "=" * 60)
    print("FILLS")
    fills = engine.trader.generate_fills_report()
    n_sell = n_buy = 0
    if fills is not None and not fills.empty:
        n_sell = int((fills["order_side"] == "SELL").sum())
        n_buy = int((fills["order_side"] == "BUY").sum())
        print(f"  total={len(fills)}  SELL(open)={n_sell}  BUY(close)={n_buy}")
    else:
        print("  no fills")

    print("\nPOSITIONS")
    positions = engine.trader.generate_positions_report()
    n_closed = 0
    pnls = pd.Series(dtype=float)
    entry_px = exit_px = None
    if positions is not None and not positions.empty:
        n_closed = len(positions)

        def _num(x):
            try:
                return float(str(x).split()[0])
            except (ValueError, IndexError):
                return 0.0

        if "realized_pnl" in positions.columns:
            pnls = positions["realized_pnl"].map(_num)
        # figure out entry/exit price column names (version-dependent)
        entry_col = next((c for c in ("avg_px_open", "avg_px_entry") if c in positions.columns), None)
        exit_col = next((c for c in ("avg_px_close", "avg_px_exit") if c in positions.columns), None)
        side_col = next((c for c in ("entry", "side") if c in positions.columns), None)
        wins = int((pnls > 0).sum())
        print(f"  closed={n_closed}  wins={wins}  losses={n_closed - wins}  "
              f"sum_pnl={pnls.sum():.2f}")
        if entry_col and exit_col:
            entry_px = positions[entry_col].map(_num)
            exit_px = positions[exit_col].map(_num)
        if side_col:
            print(f"  position sides seen: {sorted(positions[side_col].astype(str).unique())}")

    # ----------------------------------------------------------------- #
    # BUG CHECKS
    # ----------------------------------------------------------------- #
    print("\n" + "=" * 60)
    print("BUG CHECKS")
    check(n_sell == n_buy, f"SELL opens ({n_sell}) == BUY closes ({n_buy}) [balanced]")
    check(n_closed == n_sell, f"closed positions ({n_closed}) == SELL opens ({n_sell}) [no orphans]")

    # Every position should have been a SHORT (opened with SELL).
    if positions is not None and not positions.empty:
        side_col = next((c for c in ("entry", "side") if c in positions.columns), None)
        if side_col:
            sides = set(positions[side_col].astype(str).str.upper())
            check(sides.issubset({"SELL", "SHORT"}),
                  f"all positions are short-side (sides={sides})")

    # PnL sign sanity: for a SHORT, realized pnl > 0  <=>  exit price < entry price.
    if entry_px is not None and exit_px is not None and not pnls.empty:
        short_dir_ok = ((pnls > 0) == (exit_px < entry_px))
        # allow exact-equal price edge cases (pnl ~ 0) to not count as mismatch
        near_zero = pnls.abs() < 1e-9
        mism = int((~short_dir_ok & ~near_zero).sum())
        check(mism == 0,
              f"PnL sign matches short direction (exit<entry => win) "
              f"[{mism} mismatches]")

    # No position should remain open at the end of the run.
    open_now = engine.cache.positions_open()
    check(len(open_now) == 0, f"no positions left open at end ({len(open_now)})")

    # Sample a few trades for eyeballing.
    if positions is not None and not positions.empty and entry_px is not None:
        print("\nSAMPLE TRADES (first 5):")
        show = positions.copy()
        show["_entry"] = entry_px
        show["_exit"] = exit_px
        show["_pnl"] = pnls
        cols = [c for c in ("ts_opened", "_entry", "_exit", "_pnl") if c in show.columns or c.startswith("_")]
        print(show[cols].head(5).to_string())

    engine.dispose()

    print("\n" + "=" * 60)
    if _PROBLEMS:
        print(f"RESULT: {len(_PROBLEMS)} PROBLEM(S) FOUND:")
        for p in _PROBLEMS:
            print(f"  - {p}")
    else:
        print("RESULT: all bug checks passed; short mechanics look correct.")
    print("=" * 60)


if __name__ == "__main__":
    main()
