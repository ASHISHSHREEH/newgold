import MetaTrader5 as mt5
from datetime import datetime, timezone

if not mt5.initialize():
    print("init failed:", mt5.last_error())
    quit()

mt5.symbol_select("GOLD", True)

for year in range(2015, 2027):
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end   = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    rates = mt5.copy_rates_range("GOLD", mt5.TIMEFRAME_M1, start, end)
    if rates is None:
        print(year, "-> error", mt5.last_error())
    else:
        print(year, "-> bars:", len(rates))

mt5.shutdown()
