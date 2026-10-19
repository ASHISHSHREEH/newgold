"""
Pull H1 history for all 6 live instruments from MT5 into per-symbol CSVs.

New/standalone: does not touch pull_h1.py (GOLD-only) or the existing catalog.
Writes to data_h1/<symbol>.csv. Read-only on MT5 (copy_rates), safe to run
while the live bot is running.

Run:
    .venv\\Scripts\\python pull_all_h1.py
"""

from __future__ import annotations

import os
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd

_ROOT = Path(__file__).resolve().parent
_ENV = _ROOT / ".env"
if _ENV.exists():
    for _line in _ENV.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _v:
                os.environ.setdefault(_k.strip(), _v.strip())

SYMBOLS = ("GOLD", "#USNDAQ100", "#USSPX500", "WTI", "BITCOIN", "#Japan225")
OUT_DIR = _ROOT / "data_h1"


def _safe(sym: str) -> str:
    return sym.replace("#", "")


def main() -> None:
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
        raise RuntimeError(f"mt5.initialize() failed: {mt5.last_error()}")

    OUT_DIR.mkdir(exist_ok=True)
    try:
        for sym in SYMBOLS:
            if not mt5.symbol_select(sym, True):
                print(f"{sym:<12} SELECT FAILED: {mt5.last_error()}")
                continue
            rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_H1, 0, 200_000)
            if rates is None or len(rates) == 0:
                print(f"{sym:<12} NO DATA: {mt5.last_error()}")
                continue
            df = pd.DataFrame(rates)
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
            df = df.rename(columns={"tick_volume": "volume"})
            df = df[["time", "open", "high", "low", "close", "volume"]]
            out = OUT_DIR / f"{_safe(sym)}_h1.csv"
            df.to_csv(out, index=False)
            print(f"{sym:<12} {len(df):>7,} bars  "
                  f"{df['time'].iloc[0]:%Y-%m-%d} .. {df['time'].iloc[-1]:%Y-%m-%d}  -> {out.name}")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
