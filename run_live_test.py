"""
run_live_test.py — same as run_live.py but uses M1 bars + EMA(3,8).

EMA(3,8) on M1 crosses every few minutes, so you can verify the full
strategy -> order -> fill path within 5-10 minutes instead of waiting
for an H1 EMA(20,50) cross.

Run:  .venv\Scripts\python run_live_test.py
Stop: Ctrl+C  (on_stop will close any open position)
"""

from __future__ import annotations

import os
from pathlib import Path

from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.live.config import LiveDataEngineConfig, LiveExecEngineConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.trading.config import ImportableStrategyConfig

from adapters.mt5.config import MT5DataClientConfig
from adapters.mt5.exec_config import MT5ExecClientConfig
from adapters.mt5.factories import MT5DataClientFactory, MT5ExecClientFactory
from adapters.mt5.instrument import build_gold_instrument

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

BAR_TYPE_STR = "GOLD.FXPRO-1-MINUTE-LAST-EXTERNAL"

DATA_CONFIG = MT5DataClientConfig(
    poll_interval_secs=0.25,
    price_precision=2,
    symbols=("GOLD",),
    terminal_path=_MT5_TERMINAL,
    login=_MT5_LOGIN,
    password=_MT5_PASSWORD,
    server=_MT5_SERVER,
)

EXEC_CONFIG = MT5ExecClientConfig(
    poll_interval_secs=1.0,
    price_precision=2,
    symbols=("GOLD",),
    terminal_path=_MT5_TERMINAL,
    login=_MT5_LOGIN,
    password=_MT5_PASSWORD,
    server=_MT5_SERVER,
)

NODE_CONFIG = TradingNodeConfig(
    trader_id="TRADER-TEST",
    logging=LoggingConfig(log_level="INFO"),
    data_engine=LiveDataEngineConfig(time_bars_timestamp_on_close=True),
    exec_engine=LiveExecEngineConfig(reconciliation=False),
    data_clients={"MT5": DATA_CONFIG},
    exec_clients={"MT5": EXEC_CONFIG},
    strategies=[
        ImportableStrategyConfig(
            strategy_path="strategy.ema_cross:EMACross",
            config_path="strategy.ema_cross:EMACrossConfig",
            config=dict(
                instrument_id="GOLD.FXPRO",
                bar_type=BAR_TYPE_STR,
                fast_ema_period=3,
                slow_ema_period=8,
                atr_period=14,
                trade_size="0.01",
                warmup_bars=30,
                enter_on_trend=True,
            ),
        )
    ],
    timeout_connection=30.0,
    timeout_reconciliation=10.0,
    timeout_portfolio=10.0,
    timeout_disconnection=10.0,
)


def main() -> None:
    node = TradingNode(config=NODE_CONFIG)
    node.add_data_client_factory("MT5", MT5DataClientFactory)
    node.add_exec_client_factory("MT5", MT5ExecClientFactory)
    instrument = build_gold_instrument()
    node.cache.add_instrument(instrument)
    node.build()
    try:
        node.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        node.stop()
        node.dispose()


if __name__ == "__main__":
    main()
