"""
Instrument definitions for FxPro MT5.

IMPORTANT: Verify exact symbol names before live trading:
    .venv\\Scripts\\python check_symbols.py

Update SYMBOL_SPECS below if any name differs from what MT5 reports.
"""

from nautilus_trader.model.currencies import USD, XAU
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import Cfd, CurrencyPair, Instrument
from nautilus_trader.model.objects import Price, Quantity

_VENUE = Venue("FXPRO")

# ── Symbol registry ──────────────────────────────────────────────────────────
# Keys are the EXACT MT5 symbol names (case-sensitive).
# Run check_symbols.py to verify these against your FxPro terminal.
#
# price_precision : decimal places in the quoted price
# size_precision  : decimal places for lot sizes  (0.01 lots → 2)
# kind            : "currency_pair" for FX/metals | "cfd" for everything else
# asset_class     : Cfd only — AssetClass enum value
# quote_currency  : Cfd only — the currency the price is denominated in
SYMBOL_SPECS: dict[str, dict] = {
    # Verified against FxPro MT5 demo via check_symbols.py
    "GOLD": {
        "kind": "currency_pair",
        "price_precision": 2, "size_precision": 2,
    },
    "#USNDAQ100": {
        "kind": "cfd",
        "price_precision": 2, "size_precision": 2,
        "asset_class": AssetClass.INDEX, "quote_currency": USD,
    },
    "#USSPX500": {
        "kind": "cfd",
        "price_precision": 2, "size_precision": 2,
        "asset_class": AssetClass.INDEX, "quote_currency": USD,
    },
    "WTI": {
        "kind": "cfd",
        "price_precision": 3, "size_precision": 2,
        "asset_class": AssetClass.COMMODITY, "quote_currency": USD,
    },
    "BITCOIN": {
        "kind": "cfd",
        "price_precision": 2, "size_precision": 2,
        "asset_class": AssetClass.CRYPTOCURRENCY, "quote_currency": USD,
    },
    "#Japan225": {
        "kind": "cfd",
        "price_precision": 2, "size_precision": 2,
        "asset_class": AssetClass.INDEX, "quote_currency": USD,
    },
}

# Derived convenience dicts used by the MT5 adapter configs.
PRICE_PRECISIONS: dict[str, int] = {s: v["price_precision"] for s, v in SYMBOL_SPECS.items()}
SIZE_PRECISIONS: dict[str, int]  = {s: v["size_precision"]  for s, v in SYMBOL_SPECS.items()}


def _price_increment(precision: int) -> Price:
    return Price.from_str(f"{10 ** -precision:.{precision}f}")


def _size_increment(precision: int) -> Quantity:
    return Quantity.from_str(f"{10 ** -precision:.{precision}f}")


def build_gold_instrument() -> CurrencyPair:
    return CurrencyPair(
        instrument_id=InstrumentId(Symbol("GOLD"), _VENUE),
        raw_symbol=Symbol("GOLD"),
        base_currency=XAU,
        quote_currency=USD,
        price_precision=2,
        size_precision=2,
        price_increment=_price_increment(2),
        size_increment=_size_increment(2),
        ts_event=0,
        ts_init=0,
    )


def build_cfd_instrument(symbol: str) -> Cfd:
    spec = SYMBOL_SPECS[symbol]
    pp = spec["price_precision"]
    sp = spec["size_precision"]
    return Cfd(
        instrument_id=InstrumentId(Symbol(symbol), _VENUE),
        raw_symbol=Symbol(symbol),
        asset_class=spec["asset_class"],
        quote_currency=spec["quote_currency"],
        price_precision=pp,
        size_precision=sp,
        price_increment=_price_increment(pp),
        size_increment=_size_increment(sp),
        ts_event=0,
        ts_init=0,
    )


def build_all_instruments() -> list[Instrument]:
    """Return a NautilusTrader Instrument for every entry in SYMBOL_SPECS."""
    result: list[Instrument] = []
    for symbol, spec in SYMBOL_SPECS.items():
        if spec["kind"] == "currency_pair":
            result.append(build_gold_instrument())
        else:
            result.append(build_cfd_instrument(symbol))
    return result
