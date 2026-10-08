import MetaTrader5 as mt5

SYMBOLS = ["GOLD", "#USNDAQ100", "#Japan225", "USDJPY", "EURUSD", "GBPUSD", "WTI"]

mt5.initialize()

for s in mt5.symbols_get():
    n = s.name.upper()
    if any(k in n for k in ("OIL", "WTI", "BRENT", "CRUDE")):
        print("oil candidate:", s.name)
print()

acc = mt5.account_info()
print(f"account: {acc.currency}  leverage 1:{acc.leverage}  equity {acc.equity}\n")

for s in SYMBOLS:
    if not mt5.symbol_select(s, True):
        print(f"{s:<12} NOT AVAILABLE")
        continue
    i = mt5.symbol_info(s)
    t = mt5.symbol_info_tick(s)
    print(f"{s}")
    print(f"  min/max/step lot : {i.volume_min} / {i.volume_max} / {i.volume_step}")
    print(f"  contract size    : {i.trade_contract_size}")
    print(f"  digits / point   : {i.digits} / {i.point}")
    print(f"  tick size/value  : {i.trade_tick_size} / {i.trade_tick_value}")
    print(f"  margin initial   : {i.margin_initial}")
    print(f"  profit currency  : {i.currency_profit}")
    print(f"  price now        : {t.bid}")
    calc = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, s, i.volume_min, t.ask)
    print(f"  margin @ min lot : {calc} {acc.currency}\n")

mt5.shutdown()