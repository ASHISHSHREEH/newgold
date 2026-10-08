from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd

from nautilus_trader.model.currencies import USD, XAU
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import CurrencyPair
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.persistence.wranglers_v2 import BarDataWranglerV2

BAR_TYPE_STR = "GOLD.FXPRO-1-HOUR-LAST-EXTERNAL"
CATALOG_PATH = Path(__file__).parent / "catalog"


def build_instrument() -> CurrencyPair:
    return CurrencyPair(
        instrument_id=InstrumentId(Symbol("GOLD"), Venue("FXPRO")),
        raw_symbol=Symbol("GOLD"),
        base_currency=XAU,
        quote_currency=USD,
        price_precision=2,
        size_precision=2,
        price_increment=Price.from_str("0.01"),
        size_increment=Quantity.from_str("0.01"),
        ts_event=0,
        ts_init=0,
    )


def main() -> None:
    # Start fresh so write_data never skips existing parquet files
    if CATALOG_PATH.exists():
        shutil.rmtree(CATALOG_PATH)
    CATALOG_PATH.mkdir(parents=True)

    # ── 1. Load CSV ──────────────────────────────────────────────────────────
    df = pd.read_csv(Path(__file__).parent / "gold_h1.csv")
    # The wrangler looks for "ts_event" (or "timestamp"); our CSV calls it "time"
    df = df.rename(columns={"time": "ts_event"})
    print(f"Loaded {len(df):,} rows from gold_h1.csv")

    # ── 2. Build instrument ───────────────────────────────────────────────────
    instrument = build_instrument()

    # ── 3. Wrangle bars ───────────────────────────────────────────────────────
    wrangler = BarDataWranglerV2(
        bar_type=BAR_TYPE_STR,
        price_precision=instrument.price_precision,
        size_precision=instrument.size_precision,
    )
    bars = wrangler.from_pandas(df)
    print(f"Wrangled {len(bars):,} bars  (bar type: {BAR_TYPE_STR})")

    # ── 4. Write to catalog ───────────────────────────────────────────────────
    catalog = ParquetDataCatalog(str(CATALOG_PATH))
    catalog.write_data([instrument])
    catalog.write_data(bars)
    print(f"Wrote instrument + bars to {CATALOG_PATH}")

    # ── 5. Read back (acceptance test) ───────────────────────────────────────
    instruments_back = catalog.instruments()
    bars_back = catalog.bars(bar_types=[BAR_TYPE_STR])

    print("\n--- Acceptance test ---")
    print(f"Instruments read back : {len(instruments_back)}")
    print(f"Bars read back        : {len(bars_back)}")
    if bars_back:
        print(f"First bar : {bars_back[0]}")
        print(f"Last bar  : {bars_back[-1]}")


if __name__ == "__main__":
    main()
