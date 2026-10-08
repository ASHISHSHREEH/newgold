import MetaTrader5 as mt5
from datetime import datetime, timezone, timedelta

if not mt5.initialize():
    print("init failed:", mt5.last_error())
    quit()

print("selected:", mt5.symbol_select("GOLD", True))
print("visible :", mt5.symbol_info("GOLD").visible)

# A: no datetimes at all
r = mt5.copy_rates_from_pos("GOLD", mt5.TIMEFRAME_M1, 0, 10)
print("A from_pos M1  :", len(r) if r is not None else mt5.last_error())

# B: no datetimes, D1
r = mt5.copy_rates_from_pos("GOLD", mt5.TIMEFRAME_D1, 0, 10)
print("B from_pos D1  :", len(r) if r is not None else mt5.last_error())

# C: single datetime, tz-aware
r = mt5.copy_rates_from("GOLD", mt5.TIMEFRAME_M1,
                        datetime.now(timezone.utc), 10)
print("C from tz-aware:", len(r) if r is not None else mt5.last_error())

# D: single datetime, naive
r = mt5.copy_rates_from("GOLD", mt5.TIMEFRAME_M1, datetime.now(), 10)
print("D from naive   :", len(r) if r is not None else mt5.last_error())

# E: range, last 24h, naive
end = datetime.now()
r = mt5.copy_rates_range("GOLD", mt5.TIMEFRAME_M1,
                         end - timedelta(days=1), end)
print("E range naive  :", len(r) if r is not None else mt5.last_error())

# F: range, last 24h, tz-aware
end = datetime.now(timezone.utc)
r = mt5.copy_rates_range("GOLD", mt5.TIMEFRAME_M1,
                         end - timedelta(days=1), end)
print("F range tzaware:", len(r) if r is not None else mt5.last_error())

mt5.shutdown()