"""
Standalone isolation test for broker-side SL/TP modification (TRADE_ACTION_SLTP).

Purpose
-------
Prove that FxPro accepts a post-entry SL/TP modification on an open position,
BEFORE any of this logic is wired into the hand-written MT5 execution adapter.
This script imports nothing from the repo and touches no live file. It talks to
the terminal directly with the MetaTrader5 package.

What it does (on your FxPro DEMO account)
-----------------------------------------
1. Connects using the same .env credentials run_live.py uses.
2. Pre-flight: symbol exists + selected, market open, reads point / digits /
   stops-level / filling mode.
3. Opens ONE tiny market position (default 0.01 lot) with NO sl/tp.
4. TRADE_ACTION_SLTP -> sets a valid SL/TP, verifies via positions_get.
5. TRADE_ACTION_SLTP again (simulates a trailing-stop move), verifies.
6. Sends a deliberately too-close SL to observe the broker's reject retcode.
7. ALWAYS closes the position in a finally block, even on error.

Safety
------
- DEMO account only. Uses the minimum lot. Always flattens what it opens.
- Does NOT require the live bot to be stopped (MT5 allows multiple processes
  to attach to one terminal), but running flat is still recommended.
- --dry-run performs steps 1-2 only (no trading).

Usage
-----
    .venv\\Scripts\\python tests\\test_sltp_modify.py
    .venv\\Scripts\\python tests\\test_sltp_modify.py --symbol GOLD --volume 0.01
    .venv\\Scripts\\python tests\\test_sltp_modify.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import MetaTrader5 as mt5

# --------------------------------------------------------------------------- #
# Credentials - load .env exactly like run_live.py
# --------------------------------------------------------------------------- #
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _v:
                os.environ.setdefault(_k.strip(), _v.strip())

MT5_LOGIN = int(os.environ["MT5_LOGIN"]) if os.environ.get("MT5_LOGIN") else None
MT5_PASSWORD = os.environ.get("MT5_PASSWORD") or None
MT5_SERVER = os.environ.get("MT5_SERVER") or None
MT5_TERMINAL = os.environ.get("MT5_TERMINAL_PATH") or None


# --------------------------------------------------------------------------- #
# Small reporting helpers
# --------------------------------------------------------------------------- #
_PASS = 0
_FAIL = 0


def ok(msg: str) -> None:
    global _PASS
    _PASS += 1
    print(f"  [PASS] {msg}")


def bad(msg: str) -> None:
    global _FAIL
    _FAIL += 1
    print(f"  [FAIL] {msg}")


def info(msg: str) -> None:
    print(f"  [info] {msg}")


def step(msg: str) -> None:
    print(f"\n=== {msg} ===")


def _retcode_name(code: int) -> str:
    """Human name for an mt5 TRADE_RETCODE_* value, best effort."""
    for name in dir(mt5):
        if name.startswith("TRADE_RETCODE_") and getattr(mt5, name) == code:
            return name
    return "UNKNOWN"


def _pick_filling_mode(sym_info) -> int:
    """
    Choose a supported filling mode from the symbol's filling_mode bitmask.
    SYMBOL_FILLING_FOK = 1, SYMBOL_FILLING_IOC = 2 (bitmask).
    Falls back to IOC (what the live adapter uses) if the mask is unexpected.
    """
    mask = getattr(sym_info, "filling_mode", 0)
    if mask & 1:
        return mt5.ORDER_FILLING_FOK
    if mask & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_IOC


# --------------------------------------------------------------------------- #
# Core actions
# --------------------------------------------------------------------------- #
def connect() -> bool:
    kwargs: dict = {}
    if MT5_TERMINAL:
        kwargs["path"] = MT5_TERMINAL
    if MT5_LOGIN is not None:
        kwargs["login"] = MT5_LOGIN
    if MT5_PASSWORD:
        kwargs["password"] = MT5_PASSWORD
    if MT5_SERVER:
        kwargs["server"] = MT5_SERVER

    if not mt5.initialize(**kwargs):
        bad(f"mt5.initialize() failed: {mt5.last_error()}")
        return False

    acct = mt5.account_info()
    if acct is None:
        bad(f"account_info() returned None: {mt5.last_error()}")
        return False

    trade_allowed = getattr(acct, "trade_allowed", True)
    ok(f"connected: login={acct.login}  server={acct.server}  "
       f"balance={acct.balance} {acct.currency}  trade_allowed={trade_allowed}")
    if "demo" not in str(acct.server).lower() and acct.trade_mode != 0:
        # trade_mode 0 == ACCOUNT_TRADE_MODE_DEMO
        info(f"WARNING: account trade_mode={acct.trade_mode} (0=demo). "
             "This script opens a REAL position - confirm this is a demo account.")
    return True


def preflight(symbol: str):
    """Return symbol_info or None. Selects the symbol and checks market state."""
    si = mt5.symbol_info(symbol)
    if si is None:
        bad(f"symbol_info({symbol!r}) is None - symbol not found on this account")
        return None

    if not si.visible:
        if not mt5.symbol_select(symbol, True):
            bad(f"symbol_select({symbol!r}) failed: {mt5.last_error()}")
            return None
        info(f"symbol {symbol} selected into Market Watch")
        si = mt5.symbol_info(symbol)

    tick = mt5.symbol_info_tick(symbol)
    if tick is None or (tick.bid == 0.0 and tick.ask == 0.0):
        bad(f"no tick for {symbol} - market data unavailable")
        return None

    # TRADE_MODE_FULL == 4 means trading fully enabled
    tm_full = getattr(mt5, "SYMBOL_TRADE_MODE_FULL", 4)
    if si.trade_mode != tm_full:
        bad(f"{symbol} trade_mode={si.trade_mode} (need {tm_full}=FULL) - "
            "market likely closed or symbol disabled")
        return None

    ok(f"{symbol}: bid={tick.bid} ask={tick.ask} point={si.point} "
       f"digits={si.digits} stops_level={si.trade_stops_level} "
       f"vol_min={si.volume_min} filling_mask={getattr(si, 'filling_mode', '?')}")
    return si


def open_position(symbol: str, volume: float, si) -> int | None:
    """Open a BUY market position with NO sl/tp. Returns the position ticket."""
    tick = mt5.symbol_info_tick(symbol)
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": mt5.ORDER_TYPE_BUY,
        "price": tick.ask,
        "deviation": 50,
        "comment": "sltp_test_open",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _pick_filling_mode(si),
    }
    res = mt5.order_send(request)
    if res is None:
        bad(f"open order_send returned None: {mt5.last_error()}")
        return None
    if res.retcode != mt5.TRADE_RETCODE_DONE:
        bad(f"open rejected: retcode {res.retcode} ({_retcode_name(res.retcode)}) "
            f"- {res.comment}")
        # Retry once with the other filling mode if it was a filling problem
        if res.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
            alt = (mt5.ORDER_FILLING_FOK
                   if request["type_filling"] == mt5.ORDER_FILLING_IOC
                   else mt5.ORDER_FILLING_IOC)
            info(f"retrying open with alternate filling mode {alt}")
            request["type_filling"] = alt
            res = mt5.order_send(request)
            if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
                bad(f"retry open failed: "
                    f"{getattr(res, 'retcode', None)} {getattr(res, 'comment', mt5.last_error())}")
                return None
        else:
            return None

    # In retail hedging, the new position ticket equals the order ticket.
    ticket = res.order
    ok(f"opened position ticket={ticket} vol={res.volume} @ {res.price}")
    return ticket


def get_position(ticket: int):
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return None
    return positions[0]


def modify_sltp(ticket: int, sl: float | None, tp: float | None, digits: int) -> tuple[bool, int, str]:
    """Send TRADE_ACTION_SLTP. Returns (ok, retcode, comment)."""
    pos = get_position(ticket)
    if pos is None:
        return (False, -1, f"position {ticket} not found")
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": pos.symbol,
        "position": ticket,
    }
    if sl is not None:
        request["sl"] = round(sl, digits)
    if tp is not None:
        request["tp"] = round(tp, digits)
    res = mt5.order_send(request)
    if res is None:
        return (False, -1, f"order_send(SLTP) returned None: {mt5.last_error()}")
    return (res.retcode == mt5.TRADE_RETCODE_DONE, res.retcode, res.comment)


def close_position(ticket: int, si) -> bool:
    pos = get_position(ticket)
    if pos is None:
        info(f"position {ticket} already gone - nothing to close")
        return True
    symbol = pos.symbol
    tick = mt5.symbol_info_tick(symbol)
    # Close a BUY with a SELL at bid, pinned to the position ticket (hedging).
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": pos.volume,
        "type": mt5.ORDER_TYPE_SELL,
        "position": ticket,
        "price": tick.bid,
        "deviation": 50,
        "comment": "sltp_test_close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _pick_filling_mode(si),
    }
    res = mt5.order_send(request)
    if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
        bad(f"CLOSE FAILED ticket={ticket}: "
            f"{getattr(res, 'retcode', None)} "
            f"({_retcode_name(getattr(res, 'retcode', -1))}) "
            f"{getattr(res, 'comment', mt5.last_error())}")
        return False
    ok(f"closed position ticket={ticket} @ {res.price}")
    return True


# --------------------------------------------------------------------------- #
# Main test flow
# --------------------------------------------------------------------------- #
def run(symbol: str, volume: float, dry_run: bool) -> int:
    step("STEP 1 - connect")
    if not connect():
        return 1

    try:
        step(f"STEP 2 - pre-flight {symbol}")
        si = preflight(symbol)
        if si is None:
            return 1

        if dry_run:
            info("--dry-run: skipping all trading steps")
            return 0

        if volume < si.volume_min:
            info(f"requested volume {volume} < vol_min {si.volume_min}; using {si.volume_min}")
            volume = si.volume_min

        ticket = None
        try:
            step("STEP 3 - open tiny position (no sl/tp)")
            ticket = open_position(symbol, volume, si)
            if ticket is None:
                return 1
            time.sleep(0.5)
            pos = get_position(ticket)
            if pos is None:
                bad("position not found right after open")
                return 1
            info(f"initial state: sl={pos.sl} tp={pos.tp} price_open={pos.price_open}")
            if pos.sl == 0.0 and pos.tp == 0.0:
                ok("opened cleanly with no venue-side sl/tp (as intended)")

            # Distance comfortably beyond any stops/freeze level.
            price = pos.price_open
            min_dist = max(price * 0.004, (si.trade_stops_level + 10) * si.point)
            info(f"using stop distance {min_dist:.{si.digits}f} "
                 f"({(min_dist / si.point):.0f} points)")

            step("STEP 4 - set valid SL/TP via TRADE_ACTION_SLTP")
            sl1 = price - min_dist
            tp1 = price + 2 * min_dist
            good, rc, cmt = modify_sltp(ticket, sl1, tp1, si.digits)
            if not good:
                bad(f"SLTP set rejected: retcode {rc} ({_retcode_name(rc)}) - {cmt}")
                return 1
            ok(f"SLTP set accepted: retcode {rc} ({_retcode_name(rc)})")
            time.sleep(0.5)
            pos = get_position(ticket)
            if pos and abs(pos.sl - round(sl1, si.digits)) < si.point / 2 \
                    and abs(pos.tp - round(tp1, si.digits)) < si.point / 2:
                ok(f"verified on venue: sl={pos.sl} tp={pos.tp}")
            else:
                bad(f"venue did not reflect the new sl/tp: "
                    f"got sl={getattr(pos, 'sl', None)} tp={getattr(pos, 'tp', None)}, "
                    f"wanted sl={round(sl1, si.digits)} tp={round(tp1, si.digits)}")

            step("STEP 5 - move SL up (simulate trailing) via TRADE_ACTION_SLTP")
            sl2 = price - min_dist * 0.5  # tighter stop, still below price
            good, rc, cmt = modify_sltp(ticket, sl2, tp1, si.digits)
            if not good:
                bad(f"trailing modify rejected: retcode {rc} ({_retcode_name(rc)}) - {cmt}")
            else:
                ok(f"trailing modify accepted: retcode {rc} ({_retcode_name(rc)})")
                time.sleep(0.5)
                pos = get_position(ticket)
                if pos and abs(pos.sl - round(sl2, si.digits)) < si.point / 2:
                    ok(f"verified trailing update on venue: sl={pos.sl}")
                else:
                    bad(f"venue sl not updated: got {getattr(pos, 'sl', None)}, "
                        f"wanted {round(sl2, si.digits)}")

            step("STEP 6 - deliberately too-close SL (expect reject)")
            tick = mt5.symbol_info_tick(symbol)
            too_close = tick.bid - si.point  # 1 point below market
            good, rc, cmt = modify_sltp(ticket, too_close, tp1, si.digits)
            if good:
                info(f"broker ACCEPTED a 1-point SL (stops_level={si.trade_stops_level}); "
                     "no hard min-distance enforced on this symbol")
            else:
                ok(f"broker correctly rejected too-close SL: "
                   f"retcode {rc} ({_retcode_name(rc)}) - {cmt}")
            # restore a sane stop before we close (cosmetic)
            modify_sltp(ticket, sl2, tp1, si.digits)

            return 0

        finally:
            step("STEP 7 - cleanup (always runs)")
            if ticket is not None:
                # retry close a few times in case of a transient requote
                for attempt in range(3):
                    if close_position(ticket, si):
                        break
                    info(f"close retry {attempt + 1}/3")
                    time.sleep(1.0)
                else:
                    bad(f"!!! MANUAL ACTION NEEDED: position {ticket} may still be OPEN")
    finally:
        mt5.shutdown()


def main() -> int:
    ap = argparse.ArgumentParser(description="Isolated TRADE_ACTION_SLTP test (FxPro demo)")
    ap.add_argument("--symbol", default="GOLD", help="MT5 symbol (default: GOLD)")
    ap.add_argument("--volume", type=float, default=0.01, help="lot size (default: 0.01)")
    ap.add_argument("--dry-run", action="store_true", help="connect + preflight only, no trading")
    args = ap.parse_args()

    print("=" * 64)
    print(" TRADE_ACTION_SLTP isolation test - FxPro demo")
    print(f" symbol={args.symbol}  volume={args.volume}  dry_run={args.dry_run}")
    print("=" * 64)

    rc = run(args.symbol, args.volume, args.dry_run)

    print("\n" + "=" * 64)
    print(f" RESULT: {_PASS} passed, {_FAIL} failed  ->  "
          f"{'OK' if (_FAIL == 0 and rc == 0) else 'PROBLEMS'}")
    print("=" * 64)
    return 1 if (_FAIL > 0 or rc != 0) else 0


if __name__ == "__main__":
    sys.exit(main())
