"""
Live trading node — EMA crossover on 6 instruments via FxPro MT5.

Instruments  : GOLD, NAS100, SP500, OIL, BTCUSD, JPN225
Bar timeframe: M15
Position caps: max 2 per instrument, max 10 total

Prerequisites
-------------
1. Run check_symbols.py to verify the exact FxPro symbol names and update
   SYMBOL_SPECS in adapters/mt5/instrument.py if any differ.
2. MetaTrader5 terminal open and logged into FxPro demo, Algo Trading enabled.
3. .venv activated.

Run:
    .venv\\Scripts\\python run_live.py

Stop with Ctrl+C — the strategy closes all open positions on shutdown.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.live.config import LiveDataEngineConfig, LiveExecEngineConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.trading.config import ImportableStrategyConfig

from adapters.mt5.config import MT5DataClientConfig
from adapters.mt5.exec_config import MT5ExecClientConfig
from adapters.mt5.factories import MT5DataClientFactory, MT5ExecClientFactory
from adapters.mt5.instrument import PRICE_PRECISIONS, SIZE_PRECISIONS, build_all_instruments
from monitor_actor import MonitorActor, MonitorActorConfig

# ── Credentials ──────────────────────────────────────────────────────────────
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _v:
                os.environ.setdefault(_k.strip(), _v.strip())

_MT5_LOGIN    = int(os.environ["MT5_LOGIN"]) if os.environ.get("MT5_LOGIN") else None
_MT5_PASSWORD = os.environ.get("MT5_PASSWORD") or None
_MT5_SERVER   = os.environ.get("MT5_SERVER") or None
_MT5_TERMINAL = os.environ.get("MT5_TERMINAL_PATH") or None

# ── Instrument / symbol configuration ────────────────────────────────────────
# These must match the EXACT names in adapters/mt5/instrument.py SYMBOL_SPECS.
# Verify with: .venv\Scripts\python check_symbols.py
SYMBOLS = ("GOLD", "#USNDAQ100", "#USSPX500", "WTI", "BITCOIN", "#Japan225")

BAR_SPEC = "15-MINUTE-LAST-EXTERNAL"

# Lot sizes per instrument (adjust to match your account margin requirements)
TRADE_SIZES: dict[str, str] = {
    "GOLD.FXPRO":        "0.01",
    "#USNDAQ100.FXPRO":  "0.03",
    "#USSPX500.FXPRO":   "0.15",
    "WTI.FXPRO":         "0.1",
    "BITCOIN.FXPRO":     "0.01",
    "#Japan225.FXPRO":   "0.1",
}

# ── Client configs ────────────────────────────────────────────────────────────
_MT5_COMMON = dict(
    terminal_path=_MT5_TERMINAL,
    login=_MT5_LOGIN,
    password=_MT5_PASSWORD,
    server=_MT5_SERVER,
    symbols=SYMBOLS,
    price_precisions=PRICE_PRECISIONS,
    size_precisions=SIZE_PRECISIONS,
)

DATA_CONFIG = MT5DataClientConfig(poll_interval_secs=0.25, **_MT5_COMMON)
EXEC_CONFIG = MT5ExecClientConfig(poll_interval_secs=1.0,  **_MT5_COMMON)

# ── Strategy ──────────────────────────────────────────────────────────────────
_STRATEGY_PARAMS = dict(
    instruments=tuple(f"{sym}.FXPRO" for sym in SYMBOLS),
    bar_spec=BAR_SPEC,
    oms_type="HEDGING",    # FxPro is a retail hedging account (not netting)
    fast_ema_period=20,
    slow_ema_period=50,
    atr_period=14,
    trade_sizes=TRADE_SIZES,
    warmup_bars=200,
    max_per_instrument=2,
    max_total=10,
    enter_on_trend=True,   # enter immediately at startup if already trending
    sl_atr_mult=1.5,       # stop-loss at 1.5× ATR below entry
    tp_atr_mult=3.0,       # take-profit at 3× ATR above entry
)

# ── Node ──────────────────────────────────────────────────────────────────────
NODE_CONFIG = TradingNodeConfig(
    trader_id="TRADER-001",
    logging=LoggingConfig(log_level="INFO"),
    data_engine=LiveDataEngineConfig(time_bars_timestamp_on_close=True),
    exec_engine=LiveExecEngineConfig(reconciliation=False),
    data_clients={"MT5": DATA_CONFIG},
    exec_clients={"MT5": EXEC_CONFIG},
    strategies=[
        ImportableStrategyConfig(
            strategy_path="strategy.ema_cross:EMACross",
            config_path="strategy.ema_cross:EMACrossConfig",
            config=_STRATEGY_PARAMS,
        )
    ],
    timeout_connection=30.0,
    timeout_reconciliation=10.0,
    timeout_portfolio=10.0,
    timeout_disconnection=10.0,
)


def _install_shutdown_handlers() -> None:
    """Route stop signals to KeyboardInterrupt so node.run()'s graceful
    shutdown (which closes all open positions) runs.

    On Windows, Nautilus does NOT set up asyncio signal handling (see
    nautilus_trader/system/kernel.py: `if platform.system() != "Windows"`),
    so the node relies on this. The watchdog sends CTRL_BREAK_EVENT (-> SIGBREAK)
    for a requested/graceful restart; SIGINT keeps manual Ctrl+C working.
    """
    def _request_shutdown(_signum, _frame):
        raise KeyboardInterrupt

    for _name in ("SIGINT", "SIGBREAK", "SIGTERM"):
        _sig = getattr(signal, _name, None)
        if _sig is not None:
            try:
                signal.signal(_sig, _request_shutdown)
            except (ValueError, OSError):
                pass  # not supported on this platform / not main thread


def main() -> None:
    _install_shutdown_handlers()

    node = TradingNode(config=NODE_CONFIG)
    node.add_data_client_factory("MT5", MT5DataClientFactory)
    node.add_exec_client_factory("MT5", MT5ExecClientFactory)

    for instrument in build_all_instruments():
        node.cache.add_instrument(instrument)

    node.build()

    monitor = MonitorActor(MonitorActorConfig(
        component_id="MonitorActor-001",
        instruments=tuple(f"{sym}.FXPRO" for sym in SYMBOLS),
        bar_spec=BAR_SPEC,
        venue="MT5",
    ))
    node.trader.add_actor(monitor)

    try:
        node.run()
    except KeyboardInterrupt:
        print("\nStopping trading node...")
    finally:
        node.stop()
        node.dispose()


if __name__ == "__main__":
    main()
