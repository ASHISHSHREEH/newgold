"""
Backtest the EMA crossover strategy on GOLD H1 data from the catalog.

Run:
    .venv\\Scripts\\python run_backtest.py          # first run (full history)
    .venv\\Scripts\\python run_backtest.py          # subsequent runs (new bars only)
    .venv\\Scripts\\python run_backtest.py --full   # force full history re-run

On each run the last bar's timestamp is saved to catalog/.last_run_ts.
Subsequent runs load only bars from (last_ts - warmup window) onwards so that
the EMA indicators can warm up but old bars are not re-processed.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.model.currencies import JPY, USD
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.risk.config import RiskEngineConfig

from adapters.mt5.instrument import build_gold_instrument
from strategy.ema_cross import EMACross, EMACrossConfig

BAR_TYPE_STR = "GOLD.FXPRO-1-HOUR-LAST-EXTERNAL"
CATALOG_PATH = Path(__file__).parent / "catalog"
VENUE = Venue("FXPRO")
LAST_RUN_FILE = CATALOG_PATH / ".last_run_ts"

# Must match EMACrossConfig.warmup_bars
_WARMUP_BARS = 200


def _ns_to_dt(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _load_last_ts() -> int | None:
    try:
        return int(LAST_RUN_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _save_last_ts(ts_ns: int) -> None:
    LAST_RUN_FILE.write_text(str(ts_ns))


def main() -> None:
    force_full = "--full" in sys.argv

    # ── 1. Load data ──────────────────────────────────────────────────────────
    catalog = ParquetDataCatalog(str(CATALOG_PATH))
    bars = catalog.bars(bar_types=[BAR_TYPE_STR])
    if not bars:
        raise RuntimeError("No bars in catalog — run pull_h1.py then build_catalog.py first")

    last_ts = None if force_full else _load_last_ts()
    if last_ts is not None:
        # Keep warmup window before the last-run cutoff so EMAs can initialise.
        warmup_ns = int(_WARMUP_BARS * 1.5 * 3600) * 1_000_000_000
        cutoff_ns = last_ts - warmup_ns
        all_count = len(bars)
        bars = [b for b in bars if b.ts_event >= cutoff_ns]
        if not bars:
            print("No new bars since last run — nothing to do.")
            return
        print(
            f"Continuing from last run ({_ns_to_dt(last_ts)}) --"
            f" {len(bars):,}/{all_count:,} bars"
            f" ({_ns_to_dt(bars[0].ts_event)} to {_ns_to_dt(bars[-1].ts_event)})"
        )
    else:
        print(
            f"Full run -- {len(bars):,} H1 bars"
            f" ({_ns_to_dt(bars[0].ts_event)} to {_ns_to_dt(bars[-1].ts_event)})"
        )

    # ── 2. Engine ─────────────────────────────────────────────────────────────
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            logging=LoggingConfig(log_level="INFO"),
            # bypass=True lets market orders work with bar-only data
            # (no quote tick in cache, so notional check would fail otherwise)
            risk_engine=RiskEngineConfig(bypass=True),
        )
    )

    # ── 3. Venue ──────────────────────────────────────────────────────────────
    engine.add_venue(
        venue=VENUE,
        oms_type=OmsType.HEDGING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(1_000_000, JPY), Money(10_000, USD)],
        base_currency=None,
    )

    # ── 4. Instrument + bars ──────────────────────────────────────────────────
    engine.add_instrument(build_gold_instrument())
    engine.add_data(bars)

    # ── 5. Strategy ───────────────────────────────────────────────────────────
    config = EMACrossConfig(
        instruments=("GOLD.FXPRO",),
        bar_spec="1-HOUR-LAST-EXTERNAL",
        fast_ema_period=20,
        slow_ema_period=50,
        atr_period=14,
        trade_sizes={"GOLD.FXPRO": "0.01"},
        warmup_bars=200,
        sl_atr_mult=2.0,
        tp_atr_mult=4.0,    # 2:1 risk-reward
        breakeven_r=1.0,    # move SL to entry after 1R profit
        trail_sl=True,
    )
    engine.add_strategy(EMACross(config=config))

    # ── 6. Run ────────────────────────────────────────────────────────────────
    engine.run()

    # ── 7. Results ────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    fills = engine.trader.generate_fills_report()
    if not fills.empty:
        # In continuation mode, filter out overlap-window fills that were
        # already reported in the previous run.
        if last_ts is not None:
            import pandas as pd
            cutoff = pd.Timestamp(last_ts, unit="ns", tz="UTC")
            new_fills = fills[fills["ts_event"] > cutoff]
        else:
            new_fills = fills
        if not new_fills.empty:
            print(f"New fills since last run : {len(new_fills)}")
            print(new_fills.to_string())
        else:
            print("No new fills since last run (warmup window only).")
    else:
        print("No fills")

    account = engine.trader.generate_account_report(venue=VENUE)
    if not account.empty:
        print("\nAccount report:")
        print(account.to_string())

    engine.dispose()

    # ── 8. Save checkpoint ────────────────────────────────────────────────────
    _save_last_ts(bars[-1].ts_event)
    print(f"\nCheckpoint saved: last bar {_ns_to_dt(bars[-1].ts_event)} UTC")


if __name__ == "__main__":
    main()
