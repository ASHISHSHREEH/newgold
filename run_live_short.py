"""
Live trading node - SHORT S4-on-crypto (BITCOIN) via FxPro MT5.

This is the one short config the backtests supported: S4 (Donchian lower-band
breakdown) on BITCOIN, H1, with the validated pure 1:2 bracket exit (no
breakeven, no trail, no regime-flip exit). See docs/short_findings.md.

Position sizing: JPY risk-based (strategy/risk_sizing.py). At startup this probes
MT5 once (read-only) for BITCOIN's "JPY per 1.0 price move per 1.0 lot" constant
and the account equity, then sizes each trade to risk RISK_PCT of equity over the
1.5*ATR stop distance, floored to the lot step. If the probe fails (terminal
down / market closed) it falls back to a fixed 0.01 lot. If a single min-lot
trade would risk more than the budget, the strategy SKIPS it (risk protection).

SEPARATE from run_live.py (the live long-only system). Designed so it CAN run
alongside the long bot on the same demo account/terminal:
  - distinct trader_id ("SHORT-001") so internal state / client order ids don't
    collide with the long node ("TRADER-001");
  - HEDGING OMS, so a BITCOIN short coexists with any long on the same symbol;
  - reconciliation=False (same as the long node) -> MUST START FLAT; restarting
    with an open short orphans it;
  - NO MonitorActor attached, so it does NOT write to data/trading_mt5.db or
    data/live_state.json (the long system's dashboard is untouched).

Scope / caveats (see docs/short_findings.md):
  - The S4-on-crypto edge is BEAR-REGIME-DEPENDENT. In a rising BTC market this
    loses money. Only run it when you want short exposure to a BTC downtrend.
  - Not added to watchdog.py. Launch/stop it manually for now.

Prerequisites: MT5 terminal open + logged into FxPro demo, Algo Trading enabled,
.venv active, account FLAT on BITCOIN shorts.

Run:
    .venv\\Scripts\\python run_live_short.py
    .venv\\Scripts\\python run_live_short.py --probe   # show sizing probe, do not trade

Stop with Ctrl+C - the strategy closes its open positions on shutdown.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

import MetaTrader5 as mt5

from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.live.config import LiveDataEngineConfig, LiveExecEngineConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.trading.config import ImportableStrategyConfig

from adapters.mt5.config import MT5DataClientConfig
from adapters.mt5.exec_config import MT5ExecClientConfig
from adapters.mt5.factories import MT5DataClientFactory, MT5ExecClientFactory
from adapters.mt5.instrument import PRICE_PRECISIONS, SIZE_PRECISIONS, build_all_instruments
from strategy.risk_sizing import probe_symbol_sizing

# -- Credentials (same .env as run_live.py) ------------------------------------
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

# -- Instrument / timeframe / risk ---------------------------------------------
# S4-on-crypto only. H1 is the timeframe the edge was validated on.
SYMBOLS = ("BITCOIN",)
INSTRUMENT_ID = "BITCOIN.FXPRO"
BAR_SPEC = "1-HOUR-LAST-EXTERNAL"

RISK_PCT = 0.01   # fraction of account equity to risk per trade (over 1.5*ATR stop)

# Fallback fixed lot if the sizing probe fails.
FIXED_TRADE_SIZES: dict[str, str] = {INSTRUMENT_ID: "0.01"}

# -- Client configs (manage BITCOIN only) ---------------------------------------
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

# -- Base strategy params (sizing is merged in at startup) ----------------------
_BASE_PARAMS = dict(
    instruments=(INSTRUMENT_ID,),
    bar_spec=BAR_SPEC,
    oms_type="HEDGING",
    fast_ema_period=20,
    slow_ema_period=50,
    atr_period=14,
    warmup_bars=200,
    max_per_instrument=2,
    max_total=2,
    detectors=("S4",),
    donchian_period=20,
    sl_atr_mult=1.5,
    tp_atr_mult=3.0,
    # Validated exit for S4: pure 1:2 bracket, no breakeven, no trail, no flip.
    breakeven_r=1e9,
    trail_sl=False,
    exit_on_regime_flip=False,
    # Regime filter OFF (the simple one hurt in testing).
    regime_slope_bars=0,
    regime_require_below_slow=False,
)


def _mt5_kwargs() -> dict:
    kw: dict = {}
    if _MT5_TERMINAL:
        kw["path"] = _MT5_TERMINAL
    if _MT5_LOGIN is not None:
        kw["login"] = _MT5_LOGIN
    if _MT5_PASSWORD:
        kw["password"] = _MT5_PASSWORD
    if _MT5_SERVER:
        kw["server"] = _MT5_SERVER
    return kw


def resolve_sizing() -> dict:
    """
    Probe MT5 once (read-only) for BITCOIN's JPY/price/lot constant + volume
    bounds and the account equity, and return JPY-risk-sizing strategy kwargs.
    Falls back to fixed lots ({}-> trade_sizes) if the probe fails.

    Opens and closes its own short-lived MT5 connection; runs BEFORE the node's
    exec worker connects, so there is no cross-thread mt5 use. Does not affect the
    separate long-bot process (mt5.shutdown only closes this process's handle).
    """
    if not mt5.initialize(**_mt5_kwargs()):
        print(f"[sizing] mt5.initialize failed: {mt5.last_error()} -> FIXED 0.01 lot")
        return dict(trade_sizes=dict(FIXED_TRADE_SIZES))
    try:
        acct = mt5.account_info()
        spec = probe_symbol_sizing("BITCOIN")
        if acct is None or spec is None:
            print("[sizing] probe returned nothing -> FIXED 0.01 lot")
            return dict(trade_sizes=dict(FIXED_TRADE_SIZES))
        risk_jpy = float(acct.equity) * RISK_PCT
        print(f"[sizing] equity={acct.equity:.0f} {acct.currency}  "
              f"risk/trade={risk_jpy:.0f} JPY ({RISK_PCT:.1%})")
        print(f"[sizing] BITCOIN jpy/price/lot={spec.jpy_per_price_per_lot:.1f}  "
              f"vol[min/step/max]={spec.volume_min}/{spec.volume_step}/{spec.volume_max}")
        return dict(
            use_risk_sizing=True,
            risk_jpy_per_trade=risk_jpy,
            jpy_per_price_per_lot={INSTRUMENT_ID: spec.jpy_per_price_per_lot},
            volume_min={INSTRUMENT_ID: spec.volume_min},
            volume_step={INSTRUMENT_ID: spec.volume_step},
            volume_max={INSTRUMENT_ID: spec.volume_max},
            # keep a fixed fallback too, in case a per-signal spec is missing
            trade_sizes=dict(FIXED_TRADE_SIZES),
        )
    finally:
        mt5.shutdown()


def build_node_config(sizing_kwargs: dict) -> TradingNodeConfig:
    params = dict(_BASE_PARAMS)
    params.update(sizing_kwargs)
    return TradingNodeConfig(
        trader_id="SHORT-001",   # distinct from the long node's TRADER-001
        logging=LoggingConfig(log_level="INFO"),
        data_engine=LiveDataEngineConfig(time_bars_timestamp_on_close=True),
        exec_engine=LiveExecEngineConfig(reconciliation=False),
        data_clients={"MT5": DATA_CONFIG},
        exec_clients={"MT5": EXEC_CONFIG},
        strategies=[
            ImportableStrategyConfig(
                strategy_path="strategy.multi_short:MultiShortEMA",
                config_path="strategy.multi_short:MultiShortConfig",
                config=params,
            )
        ],
        timeout_connection=30.0,
        timeout_reconciliation=10.0,
        timeout_portfolio=10.0,
        timeout_disconnection=10.0,
    )


def _install_shutdown_handlers() -> None:
    """Route stop signals to KeyboardInterrupt so node.run()'s graceful shutdown
    (which closes open positions) runs. Mirrors run_live.py; Nautilus skips
    asyncio signal setup on Windows."""
    def _request_shutdown(_signum, _frame):
        raise KeyboardInterrupt

    for _name in ("SIGINT", "SIGBREAK", "SIGTERM"):
        _sig = getattr(signal, _name, None)
        if _sig is not None:
            try:
                signal.signal(_sig, _request_shutdown)
            except (ValueError, OSError):
                pass


def main() -> None:
    probe_only = "--probe" in sys.argv

    sizing = resolve_sizing()
    if probe_only:
        mode = "RISK-SIZED" if sizing.get("use_risk_sizing") else "FIXED 0.01 lot"
        print(f"[probe] sizing mode: {mode}")
        return

    _install_shutdown_handlers()

    node = TradingNode(config=build_node_config(sizing))
    node.add_data_client_factory("MT5", MT5DataClientFactory)
    node.add_exec_client_factory("MT5", MT5ExecClientFactory)

    for instrument in build_all_instruments():
        node.cache.add_instrument(instrument)

    node.build()

    # NOTE: intentionally NO MonitorActor here (keeps the long system's
    # dashboard DB/state untouched). This node runs headless via logs.

    try:
        node.run()
    except KeyboardInterrupt:
        print("\nStopping short trading node...")
    finally:
        node.stop()
        node.dispose()


if __name__ == "__main__":
    main()
