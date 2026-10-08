"""
Run this BEFORE configuring multi-instrument trading to find the exact
FxPro MT5 symbol names, precisions, and lot steps.

    .venv/Scripts/python check_symbols.py

Look for the symbols matching GOLD, NASDAQ, S&P500, Oil, Bitcoin, Japan225.
Copy the exact names (case-sensitive) into SYMBOL_SPECS in adapters/mt5/instrument.py.
"""

import os
from pathlib import Path

import MetaTrader5 as mt5

_env = Path(".env")
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _v:
                os.environ.setdefault(_k.strip(), _v.strip())

_kwargs: dict = {}
if os.environ.get("MT5_LOGIN"):
    _kwargs["login"] = int(os.environ["MT5_LOGIN"])
if os.environ.get("MT5_PASSWORD"):
    _kwargs["password"] = os.environ["MT5_PASSWORD"]
if os.environ.get("MT5_SERVER"):
    _kwargs["server"] = os.environ["MT5_SERVER"]

if not mt5.initialize(**_kwargs):
    print(f"mt5.initialize() failed: {mt5.last_error()}")
    raise SystemExit(1)

KEYWORDS = [
    "GOLD", "XAU",
    "NAS", "NDX", "USTEC", "US100", "US30", "DOW", "100",
    "SP5", "SPX", "US500", "USP", "USSPX",
    "OIL", "WTI", "CRUDE", "XTI", "BRENT",
    "BTC", "BIT", "CRYP", "COIN", "ETH",
    "JPN", "NIK", "JP2", "NIKKEI", "JAPAN",
]

print(f"{'Symbol':<22} {'digits':>6} {'vol_step':>10} {'calc_mode':>10}  description")
print("-" * 80)

all_symbols = mt5.symbols_get() or []
seen: set[str] = set()
for s in sorted(all_symbols, key=lambda x: x.name):
    name_upper = s.name.upper()
    for kw in KEYWORDS:
        if kw in name_upper and s.name not in seen:
            seen.add(s.name)
            info = mt5.symbol_info(s.name)
            if info:
                print(
                    f"{s.name:<22} {info.digits:>6} {info.volume_step:>10.4f}"
                    f" {info.trade_calc_mode:>10}  {info.description}"
                )
            break

mt5.shutdown()
