"""
Diagnose MT5 "AutoTrading disabled by client" (retcode 10027).

Connects with the SAME .env credentials the bot uses and reports whether the
terminal instance the Python API actually attached to allows automated trading.
The AutoTrading button you see in a window only governs THAT instance; on a VPS
there may be several terminals, or one the API launched itself. The authoritative
check is terminal_info().trade_allowed on the connected instance.

Run (on the machine running the bot):
    .venv\\Scripts\\python check_autotrading.py
"""

from __future__ import annotations

import os
from pathlib import Path

import MetaTrader5 as mt5

# -- Load .env exactly like run_live.py ----------------------------------------
_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _v:
                os.environ.setdefault(_k.strip(), _v.strip())


def _kwargs() -> dict:
    kw: dict = {}
    if os.environ.get("MT5_TERMINAL_PATH"):
        kw["path"] = os.environ["MT5_TERMINAL_PATH"]
    if os.environ.get("MT5_LOGIN"):
        kw["login"] = int(os.environ["MT5_LOGIN"])
    if os.environ.get("MT5_PASSWORD"):
        kw["password"] = os.environ["MT5_PASSWORD"]
    if os.environ.get("MT5_SERVER"):
        kw["server"] = os.environ["MT5_SERVER"]
    return kw


def main() -> int:
    print("=" * 64)
    print(" MT5 AutoTrading diagnostic (retcode 10027)")
    print("=" * 64)

    env_path = os.environ.get("MT5_TERMINAL_PATH")
    print(f".env MT5_TERMINAL_PATH : {env_path or '(not set - API auto-detects a terminal)'}")

    if not mt5.initialize(**_kwargs()):
        print(f"[FAIL] mt5.initialize() failed: {mt5.last_error()}")
        print("       -> terminal not running / wrong path / bad credentials.")
        return 1

    try:
        ti = mt5.terminal_info()
        ai = mt5.account_info()

        if ti is None:
            print(f"[FAIL] terminal_info() is None: {mt5.last_error()}")
            return 1

        print("\n-- Connected terminal instance --")
        print(f"  path          : {ti.path}")
        print(f"  name/company  : {ti.name} / {ti.company}  build {ti.build}")
        print(f"  connected     : {ti.connected}")
        print(f"  trade_allowed : {ti.trade_allowed}   <== AutoTrading on THIS instance")

        if ai is not None:
            print("\n-- Account --")
            print(f"  login         : {ai.login}  server={ai.server}")
            print(f"  trade_allowed : {ai.trade_allowed}   (False => investor/read-only login or broker-disabled)")
            print(f"  trade_expert  : {ai.trade_expert}   (False => EAs/automation blocked for this account)")
            print(f"  balance/equity: {ai.balance} / {ai.equity} {ai.currency}")

        print("\n-- Verdict --")
        if ti.trade_allowed and (ai is None or ai.trade_allowed):
            print("  [OK] Automated trading is ALLOWED on the connected terminal.")
            print("       If you still saw 10027, you enabled AutoTrading on a DIFFERENT")
            print("       terminal than this one. Orders go through THIS instance:")
            print(f"       {ti.path}")
            rc = 0
        else:
            print("  [PROBLEM] Automated trading is BLOCKED on the connected instance.")
            if not ti.trade_allowed:
                print("   -> Click the 'Algo Trading' button GREEN in the terminal at the")
                print(f"      path above (not whatever window you were looking at):")
                print(f"      {ti.path}")
                print("      Also check Tools > Options > Expert Advisors > Allow algorithmic trading.")
            if ai is not None and not ai.trade_allowed:
                print("   -> account trade_allowed is False: log in with the MASTER/trade")
                print("      password (not the investor/read-only password).")
            if ai is not None and not ai.trade_expert:
                print("   -> account trade_expert is False: automation is disabled for this")
                print("      account/server; check the broker or account type.")
            if not env_path:
                print("   -> MT5_TERMINAL_PATH is unset, so the API auto-picked a terminal.")
                print("      Set it in .env to the terminal above so you always target the")
                print("      right instance.")
            rc = 1

        print("=" * 64)
        return rc
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
