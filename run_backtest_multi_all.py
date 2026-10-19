"""
Detector shoot-out (S1 / S2 / S4) across ALL 6 instruments on H1.

Uses the separate catalog_multi/ (build with build_catalog_multi.py). For each
instrument it runs each detector separately + combined, with per-instrument bug
checks, and prints a matrix of PnL/trade and win% so detectors can be judged
where shorts actually have a chance (indices / crypto), not just on GOLD.

Multipliers: GOLD=100 (CurrencyPair, fixable). WTI contract is 1000 but Cfd
hardcodes multiplier=1, so WTI PnL is x1000 understated (win%/sign still valid;
flagged below). Indices/BTC are contract 1 -> multiplier 1 already correct.

Run:
    .venv\\Scripts\\python run_backtest_multi_all.py --start 2021-01-01 --end 2025-12-31
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

from adapters.mt5.instrument import SYMBOL_SPECS, build_cfd_instrument, build_gold_instrument
from strategy.multi_short import MultiShortEMA, MultiShortConfig

CATALOG_PATH = Path(__file__).parent / "catalog_multi"
VENUE = Venue("FXPRO")

# GOLD 100 oz/lot; WTI 1000 bbl/lot (cannot be applied - Cfd); others contract 1.
GOLD_MULT = 100
WTI_NOTE = True


def _ns_to_dt(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d")


def _to_ns(s: str) -> int:
    return int(pd.Timestamp(s, tz="UTC").value)


def _num(x) -> float:
    try:
        return float(str(x).split()[0])
    except (ValueError, IndexError):
        return 0.0


def _instrument(sym: str):
    if SYMBOL_SPECS[sym]["kind"] == "currency_pair":
        return build_gold_instrument(multiplier=GOLD_MULT)
    return build_cfd_instrument(sym)


def _bar_type(sym: str) -> str:
    return f"{sym}.FXPRO-1-HOUR-LAST-EXTERNAL"


def run_one(sym: str, detectors: tuple[str, ...], bars,
            slope_bars: int = 0, require_below: bool = False) -> dict:
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            logging=LoggingConfig(log_level="ERROR"),
            risk_engine=RiskEngineConfig(bypass=True),
        )
    )
    engine.add_venue(
        venue=VENUE, oms_type=OmsType.HEDGING, account_type=AccountType.MARGIN,
        starting_balances=[Money(1_000_000, JPY), Money(100_000, USD)],
        base_currency=None,
    )
    engine.add_instrument(_instrument(sym))
    engine.add_data(bars)
    cfg = MultiShortConfig(
        instruments=(f"{sym}.FXPRO",),
        bar_spec="1-HOUR-LAST-EXTERNAL",
        fast_ema_period=20, slow_ema_period=50, atr_period=14,
        trade_sizes={f"{sym}.FXPRO": "0.01"},
        warmup_bars=200, sl_atr_mult=1.5, tp_atr_mult=3.0,
        breakeven_r=1.0, trail_sl=True,
        detectors=detectors, pullback_atr=0.25, donchian_period=20,
        regime_slope_bars=slope_bars, regime_require_below_slow=require_below,
    )
    engine.add_strategy(MultiShortEMA(config=cfg))
    engine.run()

    fills = engine.trader.generate_fills_report()
    positions = engine.trader.generate_positions_report()
    n_sell = n_buy = n_closed = wins = 0
    pnl = 0.0
    mism = None
    if fills is not None and not fills.empty:
        n_sell = int((fills["order_side"] == "SELL").sum())
        n_buy = int((fills["order_side"] == "BUY").sum())
    if positions is not None and not positions.empty:
        n_closed = len(positions)
        pnls = positions["realized_pnl"].map(_num) if "realized_pnl" in positions.columns else pd.Series(dtype=float)
        pnl = float(pnls.sum())
        wins = int((pnls > 0).sum())
        ec = next((c for c in ("avg_px_open", "avg_px_entry") if c in positions.columns), None)
        xc = next((c for c in ("avg_px_close", "avg_px_exit") if c in positions.columns), None)
        if ec and xc:
            e = positions[ec].map(_num); x = positions[xc].map(_num)
            nz = pnls.abs() >= 1e-9
            mism = int((((pnls > 0) != (x < e)) & nz).sum())
    open_left = len(engine.cache.positions_open())
    engine.dispose()
    return {
        "sym": sym, "det": "+".join(detectors), "trades": n_closed,
        "wins": wins, "pnl": pnl, "sell": n_sell, "buy": n_buy,
        "mism": mism, "open_left": open_left,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--slope-bars", type=int, default=0,
                    help="regime: require slow EMA falling vs N bars ago (0=off)")
    ap.add_argument("--require-below", action="store_true",
                    help="regime: require close < slow EMA")
    args = ap.parse_args()

    catalog = ParquetDataCatalog(str(CATALOG_PATH))
    start_ns, end_ns = _to_ns(args.start), _to_ns(args.end)

    regime = (f"slope_bars={args.slope_bars} require_below={args.require_below}"
              if (args.slope_bars or args.require_below) else "OFF (unfiltered)")
    print(f"Detector shoot-out across 6 instruments, H1  ({args.start} .. {args.end})")
    print(f"  regime filter: {regime}")
    print("  WTI PnL is x1000 understated (Cfd multiplier limit); win%/sign still valid.\n")

    rows: list[dict] = []
    problems: list[str] = []
    for sym in SYMBOL_SPECS:
        try:
            allbars = catalog.bars(bar_types=[_bar_type(sym)])
        except Exception as e:
            print(f"{sym:<12} no bars in catalog_multi ({e})")
            continue
        bars = [b for b in allbars if start_ns <= b.ts_event <= end_ns]
        if not bars:
            print(f"{sym:<12} no bars in range")
            continue
        rng = f"{_ns_to_dt(bars[0].ts_event)}..{_ns_to_dt(bars[-1].ts_event)} ({len(bars):,})"
        print(f"{sym:<12} {rng}")
        for det in (("S1",), ("S2",), ("S4",), ("S1", "S2", "S4")):
            r = run_one(sym, det, bars, args.slope_bars, args.require_below)
            rows.append(r)
            ok = (r["sell"] == r["buy"]) and (r["trades"] == r["sell"]) \
                and (r["mism"] in (0, None)) and (r["open_left"] == 0)
            if not ok:
                problems.append(f"{sym}/{r['det']}")

    # ---- matrix ----
    print("\n" + "=" * 78)
    print(f"{'instrument':<12}{'detector':<12}{'trades':>7}{'win%':>7}{'sum_pnl':>13}{'pnl/trade':>11}")
    print("-" * 78)
    last = None
    for r in rows:
        if last is not None and r["sym"] != last:
            print("-" * 78)
        t = r["trades"]
        win = (100.0 * r["wins"] / t) if t else 0.0
        ppt = (r["pnl"] / t) if t else 0.0
        print(f"{r['sym']:<12}{r['det']:<12}{t:>7}{win:>6.1f}%{r['pnl']:>13.2f}{ppt:>11.3f}")
        last = r["sym"]
    print("=" * 78)

    print("\nBUG CHECKS (per instrument/detector): "
          + ("ALL OK" if not problems else f"PROBLEMS: {problems}"))


if __name__ == "__main__":
    main()
