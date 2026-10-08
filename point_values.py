import MetaTrader5 as mt5

SYMBOLS = ["GOLD", "#USNDAQ100", "#Japan225", "USDJPY", "EURUSD", "GBPUSD", "WTI"]

mt5.initialize()
acc = mt5.account_info()
risk = acc.equity * 0.0075
print(f"equity {acc.equity} {acc.currency} | 0.75% risk = {risk:.0f} {acc.currency}\n")

for s in SYMBOLS:
    mt5.symbol_select(s, True)
    i, t = mt5.symbol_info(s), mt5.symbol_info_tick(s)
    if not t or t.ask == 0:
        print(f"{s:<12} no price"); continue
    move = 10 * i.point * (10 ** (i.digits - 2))   # a "typical" small move
    p = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, s, i.volume_min,
                              t.ask, t.ask + move)
    print(f"{s:<12} 0.01 lot | {move:.5f} move = {p} {acc.currency}")

mt5.shutdown()