"""
Exit-logic diagnostic: test exit variants on the diagnostic cells and measure
avg win size vs avg loss size to confirm/refute the "winners cut, losers run"
hypothesis (50% win rate but still losing).

Cells (instrument, detector) are chosen as:
  - #USNDAQ100 S1, #USSPX500 S1  : the "high win% but losing" pathology
  - BITCOIN S4                   : the one robust positive edge (must not break)
  - BITCOIN S1

Variants (exit logic only; entries identical):
  V0 baseline     : breakeven_r=1.0, trail=on,  regime_exit=on   (current)
  V1 bracket+flip : breakeven_r=inf, trail=off, regime_exit=on   (let 1:2 run)
  V2 bracket only : breakeven_r=inf, trail=off, regime_exit=off  (pure 1:2 bracket)
  V3 late-BE      : breakeven_r=2.0, trail=on,  regime_exit=on
  V4 BE no-trail  : breakeven_r=1.0, trail=off, regime_exit=on

Run:
    .venv\\Scripts\\python run_exit_variants.py --start 2022-01-01 --end 2022-12-31
"""

from __future__ import annotations

import argparse
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
GOLD_MULT = 100

CELLS = [("#USNDAQ100", "S1"), ("#USSPX500", "S1"), ("BITCOIN", "S4"), ("BITCOIN", "S1")]

VARIANTS = {
    "V0 baseline":     dict(breakeven_r=1.0,  trail_sl=True,  exit_on_regime_flip=True),
    "V1 bracket+flip": dict(breakeven_r=1e9,  trail_sl=False, exit_on_regime_flip=True),
    "V2 bracket only": dict(breakeven_r=1e9,  trail_sl=False, exit_on_regime_flip=False),
    "V3 late-BE":      dict(breakeven_r=2.0,  trail_sl=True,  exit_on_regime_flip=True),
    "V4 BE no-trail":  dict(breakeven_r=1.0,  trail_sl=False, exit_on_regime_flip=True),
}


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


def run(sym: str, detector: str, bars, exit_kwargs: dict) -> dict:
    engine = BacktestEngine(config=BacktestEngineConfig(
        logging=LoggingConfig(log_level="ERROR"),
        risk_engine=RiskEngineConfig(bypass=True),
    ))
    engine.add_venue(venue=VENUE, oms_type=OmsType.HEDGING, account_type=AccountType.MARGIN,
                     starting_balances=[Money(1_000_000, JPY), Money(100_000, USD)],
                     base_currency=None)
    engine.add_instrument(_instrument(sym))
    engine.add_data(bars)
    cfg = MultiShortConfig(
        instruments=(f"{sym}.FXPRO",), bar_spec="1-HOUR-LAST-EXTERNAL",
        fast_ema_period=20, slow_ema_period=50, atr_period=14,
        trade_sizes={f"{sym}.FXPRO": "0.01"}, warmup_bars=200,
        sl_atr_mult=1.5, tp_atr_mult=3.0, detectors=(detector,),
        pullback_atr=0.25, donchian_period=20, **exit_kwargs,
    )
    engine.add_strategy(MultiShortEMA(config=cfg))
    engine.run()
    pos = engine.trader.generate_positions_report()
    trades = wins = 0
    pnl = aw = al = 0.0
    if pos is not None and not pos.empty and "realized_pnl" in pos.columns:
        p = pos["realized_pnl"].map(_num)
        trades = len(p)
        wins = int((p > 0).sum())
        pnl = float(p.sum())
        win_p = p[p > 0]
        los_p = p[p < 0]
        aw = float(win_p.mean()) if not win_p.empty else 0.0
        al = float(los_p.mean()) if not los_p.empty else 0.0
    engine.dispose()
    ppt = pnl / trades if trades else 0.0
    winpct = 100.0 * wins / trades if trades else 0.0
    # payoff ratio = avg win / abs(avg loss)
    payoff = (aw / abs(al)) if al != 0 else 0.0
    return dict(trades=trades, winpct=winpct, pnl=pnl, ppt=ppt,
                avg_win=aw, avg_loss=al, payoff=payoff)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2022-12-31")
    args = ap.parse_args()

    catalog = ParquetDataCatalog(str(CATALOG_PATH))
    start_ns, end_ns = _to_ns(args.start), _to_ns(args.end)

    print(f"EXIT-LOGIC diagnostic  ({args.start} .. {args.end})")
    print("  payoff = avg_win / |avg_loss|.  1:2 bracket's theoretical max payoff ~ 2.0")
    print("  WTI not included; crypto/index cells only.\n")

    for sym, det in CELLS:
        bars = [b for b in catalog.bars(bar_types=[f"{sym}.FXPRO-1-HOUR-LAST-EXTERNAL"])
                if start_ns <= b.ts_event <= end_ns]
        if not bars:
            print(f"{sym} {det}: no bars"); continue
        print("=" * 86)
        print(f"{sym}  detector={det}   ({len(bars):,} bars)")
        print(f"  {'variant':<16}{'trades':>7}{'win%':>7}{'avg_win':>10}{'avg_loss':>10}"
              f"{'payoff':>8}{'pnl/trade':>11}{'sum_pnl':>11}")
        print("  " + "-" * 82)
        for name, kw in VARIANTS.items():
            r = run(sym, det, bars, kw)
            print(f"  {name:<16}{r['trades']:>7}{r['winpct']:>6.1f}%"
                  f"{r['avg_win']:>10.2f}{r['avg_loss']:>10.2f}{r['payoff']:>8.2f}"
                  f"{r['ppt']:>11.3f}{r['pnl']:>11.2f}")
    print("=" * 86)


if __name__ == "__main__":
    main()
