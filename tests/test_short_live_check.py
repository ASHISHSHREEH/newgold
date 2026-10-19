"""
PRE-DEPLOYMENT LIVE SHORT CHECK (FxPro demo).

The backtests never touch the broker, and the earlier live tests opened LONGs and
only exercised SL/TP modify. This drives a real SHORT through the actual
MT5ExecutionClient._submit_order path - the exact code a deployed short strategy
would use - and verifies:

  1. SELL-to-open fills and creates a short position at the venue.
  2. A SHORT bracket is accepted: SL ABOVE price, TP BELOW price (tags on entry).
  3. reduce-only BUY-to-close (position pinned to the venue ticket) flattens it.
  4. No position is left open (no orphan).

Safety: DEMO, 0.01 lot GOLD, and a raw-mt5 force-close backstop in finally so a
position can NEVER be orphaned even if the client path errors.

Run:
    .venv\\Scripts\\python tests\\test_short_live_check.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import MetaTrader5 as mt5

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from nautilus_trader.common.providers import InstrumentProvider  # noqa: E402
from nautilus_trader.core.uuid import UUID4  # noqa: E402
from nautilus_trader.execution.messages import SubmitOrder  # noqa: E402
from nautilus_trader.model.enums import OrderSide  # noqa: E402
from nautilus_trader.model.identifiers import ClientId, ClientOrderId, PositionId  # noqa: E402
from nautilus_trader.test_kit.stubs.component import TestComponentStubs as T  # noqa: E402

from adapters.mt5.exec_config import MT5ExecClientConfig  # noqa: E402
from adapters.mt5.execution import MT5ExecutionClient  # noqa: E402
from adapters.mt5.instrument import build_gold_instrument  # noqa: E402

_ENV = _REPO / ".env"
if _ENV.exists():
    for _l in _ENV.read_text(encoding="utf-8").splitlines():
        _l = _l.strip()
        if _l and not _l.startswith("#") and "=" in _l:
            _k, _, _v = _l.partition("=")
            if _v:
                os.environ.setdefault(_k.strip(), _v.strip())

SYMBOL = "GOLD"
OPEN_COMMENT = "SHORTCHK-OPEN"

_PASS = 0
_FAIL = 0


def ok(m):
    global _PASS; _PASS += 1; print(f"  [PASS] {m}")


def bad(m):
    global _FAIL; _FAIL += 1; print(f"  [FAIL] {m}")


def info(m): print(f"  [info] {m}")


def step(m): print(f"\n=== {m} ===")


def _find_short(comment_prefix: str):
    """Return the mt5 position opened by this test (matched by comment), or None."""
    positions = mt5.positions_get(symbol=SYMBOL)
    if not positions:
        return None
    for p in positions:
        if p.comment.startswith(comment_prefix) and p.type == mt5.POSITION_TYPE_SELL:
            return p
    return None


async def run() -> int:
    loop = asyncio.get_running_loop()
    clock = T.clock()
    cache = T.cache()
    msgbus = T.msgbus()
    cfg = MT5ExecClientConfig(
        poll_interval_secs=0.25,
        login=int(os.environ["MT5_LOGIN"]) if os.environ.get("MT5_LOGIN") else None,
        password=os.environ.get("MT5_PASSWORD") or None,
        server=os.environ.get("MT5_SERVER") or None,
        terminal_path=os.environ.get("MT5_TERMINAL_PATH") or None,
        symbols=(SYMBOL,), price_precisions={SYMBOL: 2}, size_precisions={SYMBOL: 2},
    )

    step("STEP 1 - connect real MT5ExecutionClient + register GOLD")
    client = MT5ExecutionClient(loop, ClientId("MT5"), msgbus, cache, clock,
                                InstrumentProvider(), cfg)
    instrument = build_gold_instrument()
    cache.add_instrument(instrument)
    await client._connect()
    if not (client._worker and client._worker.is_alive() and client._ready.is_set()):
        bad("client did not connect"); await client._disconnect(); return 1
    ok("connected; worker alive")

    of = T.order_factory()
    trader_id = of.trader_id
    strategy_id = of.strategy_id

    ticket = None
    try:
        tick = mt5.symbol_info_tick(SYMBOL)
        si = mt5.symbol_info(SYMBOL)
        if tick is None or si.trade_mode != getattr(mt5, "SYMBOL_TRADE_MODE_FULL", 4):
            bad(f"{SYMBOL} not tradable now (market closed?)"); return 1
        price = tick.bid
        dist = max(price * 0.004, (si.trade_stops_level + 10) * si.point)
        sl = round(price + dist, si.digits)        # SHORT: stop ABOVE
        tp = round(price - 2 * dist, si.digits)     # SHORT: target BELOW
        info(f"price={price} short-bracket SL={sl} (above) TP={tp} (below)")

        step("STEP 2 - SELL to open (short) via client._submit_order")
        open_order = of.market(
            instrument_id=instrument.id,
            order_side=OrderSide.SELL,
            quantity=instrument.make_qty(0.01),
            tags=[f"SL:{sl}", f"TP:{tp}"],
            client_order_id=ClientOrderId(OPEN_COMMENT),
        )
        cache.add_order(open_order, None)
        cmd = SubmitOrder(trader_id, strategy_id, open_order, UUID4(),
                          clock.timestamp_ns())
        await client._submit_order(cmd)
        time.sleep(1.0)

        pos = _find_short(OPEN_COMMENT)
        if pos is None:
            bad("no short position found at venue after SELL submit"); return 1
        ticket = pos.ticket
        ok(f"short opened: ticket={ticket} type=SELL vol={pos.volume} @ {pos.price_open}")

        step("STEP 3 - verify SHORT bracket at venue")
        if pos.sl > pos.price_open and pos.tp < pos.price_open and pos.tp > 0:
            ok(f"venue bracket correct for a short: sl={pos.sl} > entry={pos.price_open} > tp={pos.tp}")
        else:
            bad(f"short bracket wrong: sl={pos.sl} tp={pos.tp} entry={pos.price_open}")

        step("STEP 4 - reduce-only BUY to close via client._submit_order")
        close_order = of.market(
            instrument_id=instrument.id,
            order_side=OrderSide.BUY,
            quantity=instrument.make_qty(pos.volume),
            reduce_only=True,
            client_order_id=ClientOrderId("SHORTCHK-CLOSE"),
        )
        cache.add_order(close_order, PositionId(str(ticket)))
        close_cmd = SubmitOrder(trader_id, strategy_id, close_order, UUID4(),
                                clock.timestamp_ns(), position_id=PositionId(str(ticket)))
        await client._submit_order(close_cmd)
        time.sleep(1.0)

        still = mt5.positions_get(ticket=ticket)
        if not still:
            ok(f"short closed via reduce-only BUY: ticket {ticket} gone from venue")
            ticket = None
        else:
            bad(f"short still open after close submit: {still[0].volume} lots")

        step("STEP 5 - no orphan check")
        leftover = _find_short(OPEN_COMMENT)
        if leftover is None:
            ok("no leftover SHORTCHK short at venue")
        else:
            bad(f"orphan short remains: ticket={leftover.ticket}")

        return 0
    finally:
        step("CLEANUP - raw force-close backstop (always runs)")
        p = _find_short(OPEN_COMMENT) or (mt5.positions_get(ticket=ticket)[0] if ticket and mt5.positions_get(ticket=ticket) else None)
        if p is not None:
            t = mt5.symbol_info_tick(SYMBOL)
            req = {
                "action": mt5.TRADE_ACTION_DEAL, "symbol": SYMBOL, "volume": p.volume,
                "type": mt5.ORDER_TYPE_BUY, "position": p.ticket, "price": t.ask,
                "deviation": 50, "comment": "SHORTCHK-FORCE",
                "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
            }
            r = mt5.order_send(req)
            if r and r.retcode == mt5.TRADE_RETCODE_DONE:
                info(f"force-closed leftover ticket {p.ticket}")
            else:
                bad(f"FORCE-CLOSE FAILED ticket {p.ticket}: "
                    f"{getattr(r,'retcode',None)} {getattr(r,'comment',mt5.last_error())} - CLOSE MANUALLY")
        else:
            info("nothing to force-close")
        await client._disconnect()


def main() -> int:
    print("=" * 64)
    print(" PRE-DEPLOYMENT LIVE SHORT CHECK - FxPro demo (GOLD 0.01)")
    print("=" * 64)
    rc = asyncio.run(run())
    print("\n" + "=" * 64)
    print(f" RESULT: {_PASS} passed, {_FAIL} failed  ->  "
          f"{'READY' if (_FAIL == 0 and rc == 0) else 'NOT READY'}")
    print("=" * 64)
    return 1 if (_FAIL or rc) else 0


if __name__ == "__main__":
    sys.exit(main())
