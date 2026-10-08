import MetaTrader5 as mt5
from datetime import datetime

mt5.initialize()
mt5.symbol_select("GOLD", True)

for tf_name, tf in (("M1", mt5.TIMEFRAME_M1),
                    ("M5", mt5.TIMEFRAME_M5),
                    ("H1", mt5.TIMEFRAME_H1),
                    ("D1", mt5.TIMEFRAME_D1)):
    r = mt5.copy_rates_from_pos("GOLD", tf, 0, 2_000_000)
    if r is None or len(r) == 0:
        print(tf_name, "-> none", mt5.last_error())
        continue
    print(f"{tf_name} -> {len(r):>8} bars | "
          f"{datetime.utcfromtimestamp(r[0][0])} .. "
          f"{datetime.utcfromtimestamp(r[-1][0])}")

mt5.shutdown()