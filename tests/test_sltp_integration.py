"""
Integration test for MT5ExecutionClient.modify_position_sltp().

Difference from tests/test_sltp_modify.py
------------------------------------------
test_sltp_modify.py proved the RAW mt5.order_send(TRADE_ACTION_SLTP) call works.
This test proves the SAME call works when driven through the real adapter async
path: event loop -> _req_queue -> worker thread -> _send_sltp -> future resolved
back on the loop -> modify_position_sltp() returns a bool. That is the plumbing
the short strategy will actually use.

It constructs the real MT5ExecutionClient (with Nautilus test-kit stub
msgbus/cache/clock), calls the real _connect(), and awaits
client.modify_position_sltp(...).

Scaffolding note
----------------
The position is opened/verified/closed with raw mt5 calls in the main thread
(we already proved opening works). Those calls are sequenced so they never run
while the worker thread is touching mt5. Only the MODIFY goes through the client,
because the modify path is what this test exists to validate.

Safety: DEMO account, 0.01 lot, always closed in a finally block.

Usage
-----
    .venv\\Scripts\\python tests\\test_sltp_integration.py
    .venv\\Scripts\\python tests\\test_sltp_integration.py --symbol GOLD
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

import MetaTrader5 as mt5

# Make the repo root importable (script dir is tests/, adapters/ lives one up).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from nautilus_trader.common.providers import InstrumentProvider  # noqa: E402
from nautilus_trader.model.identifiers import ClientId  # noqa: E402
from nautilus_trader.test_kit.stubs.component import TestComponentStubs as T  # noqa: E402

from adapters.mt5.exec_config import MT5ExecClientConfig  # noqa: E402
from adapters.mt5.execution import MT5ExecutionClient  # noqa: E402

# --------------------------------------------------------------------------- #
# Credentials - load .env exactly like run_live.py
# --------------------------------------------------------------------------- #
_ENV_FILE = _REPO_ROOT / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _v:
                os.environ.setdefault(_k.strip(), _v.strip())

# --------------------------------------------------------------------------- #
# Reporting helpers
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
    for name in dir(mt5):
        if name.startswith("TRADE_RETCODE_") and getattr(mt5, name) == code:
            return name
    return "UNKNOWN"


def _pick_filling_mode(si) -> int:
    mask = getattr(si, "filling_mode", 0)
    if mask & 1:
        return mt5.ORDER_FILLING_FOK
    if mask & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_IOC


# --------------------------------------------------------------------------- #
# Raw-mt5 scaffolding (main thread) - open / get / close
# --------------------------------------------------------------------------- #
def raw_open(symbol: str, volume: float, si) -> int | None:
    tick = mt5.symbol_info_tick(symbol)
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": mt5.ORDER_TYPE_BUY,
        "price": tick.ask,
        "deviation": 50,
        "comment": "sltp_integ_open",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _pick_filling_mode(si),
    }
    res = mt5.order_send(req)
    if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
        bad(f"raw open failed: {getattr(res, 'retcode', None)} "
            f"{getattr(res, 'comment', mt5.last_error())}")
        return None
    return res.order


def raw_get(ticket: int):
    positions = mt5.positions_get(ticket=ticket)
    return positions[0] if positions else None


def raw_close(ticket: int, si) -> bool:
    pos = raw_get(ticket)
    if pos is None:
        info(f"position {ticket} already gone")
        return True
    tick = mt5.symbol_info_tick(pos.symbol)
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": pos.symbol,
        "volume": pos.volume,
        "type": mt5.ORDER_TYPE_SELL,
        "position": ticket,
        "price": tick.bid,
        "deviation": 50,
        "comment": "sltp_integ_close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _pick_filling_mode(si),
    }
    res = mt5.order_send(req)
    if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
        bad(f"raw close failed ticket={ticket}: {getattr(res, 'retcode', None)} "
            f"{getattr(res, 'comment', mt5.last_error())}")
        return False
    ok(f"closed position ticket={ticket} @ {res.price}")
    return True


# --------------------------------------------------------------------------- #
# Main flow
# --------------------------------------------------------------------------- #
async def run(symbol: str, volume: float) -> int:
    loop = asyncio.get_running_loop()
    clock = T.clock()
    msgbus = T.msgbus()
    cache = T.cache()

    cfg = MT5ExecClientConfig(
        poll_interval_secs=0.25,  # snappier future resolution for the test
        login=int(os.environ["MT5_LOGIN"]) if os.environ.get("MT5_LOGIN") else None,
        password=os.environ.get("MT5_PASSWORD") or None,
        server=os.environ.get("MT5_SERVER") or None,
        terminal_path=os.environ.get("MT5_TERMINAL_PATH") or None,
        symbols=(symbol,),
        price_precisions={symbol: 2},
        size_precisions={symbol: 2},
    )

    step("STEP 1 - construct + connect real MT5ExecutionClient")
    client = MT5ExecutionClient(
        loop, ClientId("MT5"), msgbus, cache, clock, InstrumentProvider(), cfg
    )
    await client._connect()
    if not (client._worker and client._worker.is_alive() and client._ready.is_set()):
        bad("client did not connect (worker not alive / not ready)")
        await client._disconnect()
        return 1
    ok("client connected; worker thread alive and ready")

    ticket = None
    try:
        step(f"STEP 2 - pre-flight {symbol}")
        si = mt5.symbol_info(symbol)
        if si is None or not si.visible:
            mt5.symbol_select(symbol, True)
            si = mt5.symbol_info(symbol)
        tm_full = getattr(mt5, "SYMBOL_TRADE_MODE_FULL", 4)
        if si is None or si.trade_mode != tm_full:
            bad(f"{symbol} not tradable right now (market closed?) trade_mode="
                f"{getattr(si, 'trade_mode', None)}")
            return 1
        tick = mt5.symbol_info_tick(symbol)
        ok(f"{symbol}: bid={tick.bid} ask={tick.ask} point={si.point} "
           f"digits={si.digits} stops_level={si.trade_stops_level}")

        step("STEP 3 - open tiny position (raw scaffolding, no sl/tp)")
        ticket = raw_open(symbol, volume, si)
        if ticket is None:
            return 1
        time.sleep(0.5)
        pos = raw_get(ticket)
        if pos is None:
            bad("position missing right after open")
            return 1
        ok(f"opened ticket={ticket} @ {pos.price_open} (sl={pos.sl} tp={pos.tp})")

        price = pos.price_open
        dist = max(price * 0.004, (si.trade_stops_level + 10) * si.point)

        step("STEP 4 - set SL/TP THROUGH client.modify_position_sltp()")
        sl1 = round(price - dist, si.digits)
        tp1 = round(price + 2 * dist, si.digits)
        t0 = time.perf_counter()
        result = await client.modify_position_sltp(ticket, sl1, tp1)
        dt = (time.perf_counter() - t0) * 1000
        if result is not True:
            bad(f"modify_position_sltp returned {result!r} (expected True)")
            return 1
        ok(f"modify_position_sltp returned True via async path ({dt:.0f} ms round-trip)")
        time.sleep(0.5)
        pos = raw_get(ticket)
        if pos and abs(pos.sl - sl1) < si.point / 2 and abs(pos.tp - tp1) < si.point / 2:
            ok(f"venue reflects it: sl={pos.sl} tp={pos.tp}")
        else:
            bad(f"venue mismatch: got sl={getattr(pos, 'sl', None)} "
                f"tp={getattr(pos, 'tp', None)}, wanted sl={sl1} tp={tp1}")

        step("STEP 5 - move SL up (trailing) THROUGH the client")
        sl2 = round(price - dist * 0.5, si.digits)
        result = await client.modify_position_sltp(ticket, sl2, tp1)
        if result is not True:
            bad(f"trailing modify returned {result!r} (expected True)")
        else:
            ok("trailing modify returned True")
            time.sleep(0.5)
            pos = raw_get(ticket)
            if pos and abs(pos.sl - sl2) < si.point / 2:
                ok(f"venue reflects trailing update: sl={pos.sl}")
            else:
                bad(f"venue sl not updated: got {getattr(pos, 'sl', None)}, wanted {sl2}")

        step("STEP 6 - too-close SL THROUGH the client (expect graceful False)")
        tick = mt5.symbol_info_tick(symbol)
        too_close = round(tick.bid - si.point, si.digits)
        before = raw_get(ticket)
        result = await client.modify_position_sltp(ticket, too_close, tp1)
        if result is True:
            info("broker accepted a 1-point SL on this symbol; no rejection path exercised")
        elif result is False:
            ok("client returned False gracefully (no exception) on too-close SL")
            time.sleep(0.3)
            after = raw_get(ticket)
            if after and before and abs(after.sl - before.sl) < si.point / 2:
                ok(f"prior stop preserved after rejection: sl={after.sl}")
            else:
                bad(f"stop changed despite rejection: {getattr(before,'sl',None)} "
                    f"-> {getattr(after,'sl',None)}")
        else:
            bad(f"unexpected return {result!r}")

        step("STEP 7 - modify a NONEXISTENT ticket (expect graceful False)")
        result = await client.modify_position_sltp(999999999, sl2, tp1)
        if result is False:
            ok("client returned False for a missing position (no exception)")
        else:
            bad(f"expected False for missing position, got {result!r}")

        return 0

    finally:
        step("STEP 8 - cleanup (always runs)")
        if ticket is not None:
            for attempt in range(3):
                if raw_close(ticket, si):
                    break
                info(f"close retry {attempt + 1}/3")
                time.sleep(1.0)
            else:
                bad(f"!!! MANUAL ACTION NEEDED: position {ticket} may still be OPEN")
        await client._disconnect()
        ok("client disconnected")


def main() -> int:
    ap = argparse.ArgumentParser(description="Integration test for modify_position_sltp")
    ap.add_argument("--symbol", default="GOLD")
    ap.add_argument("--volume", type=float, default=0.01)
    args = ap.parse_args()

    print("=" * 64)
    print(" modify_position_sltp integration test - FxPro demo")
    print(f" symbol={args.symbol}  volume={args.volume}")
    print("=" * 64)

    rc = asyncio.run(run(args.symbol, args.volume))

    print("\n" + "=" * 64)
    print(f" RESULT: {_PASS} passed, {_FAIL} failed  ->  "
          f"{'OK' if (_FAIL == 0 and rc == 0) else 'PROBLEMS'}")
    print("=" * 64)
    return 1 if (_FAIL > 0 or rc != 0) else 0


if __name__ == "__main__":
    sys.exit(main())
