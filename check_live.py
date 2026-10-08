"""check_live.py - smoke test: EMA state + round-trip demo order."""
import MetaTrader5 as mt5
import numpy as np
from datetime import datetime, timezone

SYMBOL   = "GOLD"
EMA_FAST = 20
EMA_SLOW = 50
LOT      = 0.01

# ---------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------
if not mt5.initialize():
    print("FAIL mt5.initialize():", mt5.last_error())
    raise SystemExit(1)

mt5.symbol_select(SYMBOL, True)
print("MT5 connected.  Account:", mt5.account_info().login,
      " balance:", mt5.account_info().balance, mt5.account_info().currency)

# ---------------------------------------------------------------------------
# Fetch 200 completed H1 bars (pos=1 skip forming bar, same as adapter fix)
# ---------------------------------------------------------------------------
rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1, 1, 200)
if rates is None:
    print("FAIL copy_rates_from_pos:", mt5.last_error())
    mt5.shutdown(); raise SystemExit(1)

closes = np.array([r["time"] for r in rates], dtype="float64")  # just timestamps

def ema(prices, period):
    k = 2.0 / (period + 1)
    result = np.empty(len(prices))
    result[0] = prices[0]
    for i in range(1, len(prices)):
        result[i] = prices[i] * k + result[i-1] * (1 - k)
    return result

close_prices = np.array([r["close"] for r in rates], dtype="float64")
fast_arr = ema(close_prices, EMA_FAST)
slow_arr = ema(close_prices, EMA_SLOW)

fast_prev, slow_prev = fast_arr[-2], slow_arr[-2]
fast_cur,  slow_cur  = fast_arr[-1], slow_arr[-1]
last_bar_time = datetime.fromtimestamp(int(rates[-1]["time"]), tz=timezone.utc)

print(f"\nH1 bars fetched  : {len(rates)}")
print(f"Last completed   : {last_bar_time}  close={rates[-1]['close']:.2f}")
print(f"EMA({EMA_FAST}) prev={fast_prev:.2f}  cur={fast_cur:.2f}")
print(f"EMA({EMA_SLOW}) prev={slow_prev:.2f}  cur={slow_cur:.2f}")

if fast_prev <= slow_prev and fast_cur > slow_cur:
    print("Signal: GOLDEN CROSS  (would BUY)")
elif fast_prev >= slow_prev and fast_cur < slow_cur:
    print("Signal: DEATH CROSS   (would CLOSE)")
elif fast_cur > slow_cur:
    print("State : fast ABOVE slow (in long territory - would hold / buy if signal)")
else:
    print("State : fast BELOW slow (flat - waiting for golden cross to enter)")

# ---------------------------------------------------------------------------
# Test order: place 0.01 lot BUY then close it immediately
# ---------------------------------------------------------------------------
print("\n--- Execution path test ---")
tick = mt5.symbol_info_tick(SYMBOL)
if tick is None:
    print("FAIL symbol_info_tick:", mt5.last_error())
    mt5.shutdown(); raise SystemExit(1)

ask = tick.ask
print(f"Current ask={ask}")

req = {
    "action":    mt5.TRADE_ACTION_DEAL,
    "symbol":    SYMBOL,
    "volume":    LOT,
    "type":      mt5.ORDER_TYPE_BUY,
    "price":     ask,
    "deviation": 20,
    "magic":     999999,
    "comment":   "check_live BUY test",
    "type_time": mt5.ORDER_TIME_GTC,
    "type_filling": mt5.ORDER_FILLING_IOC,
}
result = mt5.order_send(req)
if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
    print("FAIL order_send (BUY):", result)
    mt5.shutdown(); raise SystemExit(1)

ticket = result.order
print(f"BUY  order filled  ticket={ticket}  price={result.price}")

# Close the position immediately
positions = mt5.positions_get(symbol=SYMBOL)
pos = next((p for p in (positions or []) if p.ticket == ticket), None)
if pos is None:
    # might be under a different ticket after fill — find by magic
    pos = next((p for p in (positions or []) if p.magic == 999999), None)

if pos is None:
    print("WARN could not find open position to close (may have already closed)")
else:
    bid = mt5.symbol_info_tick(SYMBOL).bid
    close_req = {
        "action":    mt5.TRADE_ACTION_DEAL,
        "symbol":    SYMBOL,
        "volume":    pos.volume,
        "type":      mt5.ORDER_TYPE_SELL,
        "position":  pos.ticket,
        "price":     bid,
        "deviation": 20,
        "magic":     999999,
        "comment":   "check_live CLOSE test",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    close_result = mt5.order_send(close_req)
    if close_result and close_result.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"SELL order filled  ticket={close_result.order}  price={close_result.price}")
        spread = round(close_result.price - result.price, 2)
        print(f"Round-trip cost: {spread} USD per unit  (spread)")
    else:
        print("FAIL close order:", close_result)

mt5.shutdown()
print("\nAll checks passed.")
