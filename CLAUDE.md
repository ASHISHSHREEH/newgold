# newgold — NautilusTrader + MT5 (FxPro demo)

## Rules
- Nautilus version is 1.231.0. Do NOT write API calls from memory.
  Read the installed package at .venv/Lib/site-packages/nautilus_trader/
  or the reference clone at D:\projects\nautilus-src before using any class.
- Do not modify anything in D:\projects\nautilus-src. Read-only reference.
- Do not vendor or copy Nautilus source into this repo. It's a pip dependency.
- MT5 connection code (adapters/mt5/) is written by hand. Do not generate it.

## Broker facts (verified, do not guess)
- Symbol: GOLD  (XAUUSD does not exist on FxPro)
- price_precision = 2, point = 0.01
- Account currency: JPY  (not USD)
- Margin mode: retail hedging -> OmsType.HEDGING (not NETTING)
- Venue: FXPRO

## Data
- Source: MetaTrader5 package, local terminal only
- History: M5/H1/D1 back to 2008-03-11; M1 at least to 2020-12
- copy_rates_range error (-2, 'Terminal: Invalid params') means
  "no data in that range", not a bad call. Prefer copy_rates_from_pos.

## Environment
- Windows, PowerShell, venv at .venv (uv-managed)
- pandas 3.x, numpy 2.x — 2.x-era examples may not work