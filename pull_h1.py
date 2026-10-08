import MetaTrader5 as mt5
import pandas as pd

mt5.initialize()
mt5.symbol_select("GOLD", True)

r = mt5.copy_rates_from_pos("GOLD", mt5.TIMEFRAME_H1, 0, 100_000)
mt5.shutdown()

df = pd.DataFrame(r)
df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
df = df.rename(columns={"tick_volume": "volume"})
df = df[["time", "open", "high", "low", "close", "volume"]]

df.to_csv("gold_h1.csv", index=False)
print(df.shape)
print(df.head(3))
print(df.tail(3))