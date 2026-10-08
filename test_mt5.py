import MetaTrader5 as mt5

if not mt5.initialize():
    print("initialize failed:", mt5.last_error())
    quit()

print("terminal:", mt5.terminal_info())
print("account:", mt5.account_info())

for sym in ("GOLD", "XAUUSD"):
    info = mt5.symbol_info(sym)
    print(sym, "->", "FOUND" if info else "not found")
    if info:
        print("   digits:", info.digits, "| point:", info.point)
        print("   tick:", mt5.symbol_info_tick(sym))

mt5.shutdown()