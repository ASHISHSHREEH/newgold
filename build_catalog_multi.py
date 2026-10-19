"""
Build a SEPARATE multi-instrument H1 catalog (catalog_multi/) from data_h1/*.csv.

Standalone: does not touch build_catalog.py or the existing GOLD-only catalog/.
Run pull_all_h1.py first.

Run:
    .venv\\Scripts\\python build_catalog_multi.py
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd

from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.persistence.wranglers_v2 import BarDataWranglerV2

from adapters.mt5.instrument import (
    SYMBOL_SPECS,
    build_cfd_instrument,
    build_gold_instrument,
)

_ROOT = Path(__file__).resolve().parent
DATA_DIR = _ROOT / "data_h1"
CATALOG_PATH = _ROOT / "catalog_multi"


def _safe(sym: str) -> str:
    return sym.replace("#", "")


def _bar_type(sym: str) -> str:
    return f"{sym}.FXPRO-1-HOUR-LAST-EXTERNAL"


def main() -> None:
    if CATALOG_PATH.exists():
        shutil.rmtree(CATALOG_PATH)
    CATALOG_PATH.mkdir(parents=True)
    catalog = ParquetDataCatalog(str(CATALOG_PATH))

    for sym, spec in SYMBOL_SPECS.items():
        csv = DATA_DIR / f"{_safe(sym)}_h1.csv"
        if not csv.exists():
            print(f"{sym:<12} SKIP (no {csv.name})")
            continue

        df = pd.read_csv(csv).rename(columns={"time": "ts_event"})
        instrument = build_gold_instrument() if spec["kind"] == "currency_pair" else build_cfd_instrument(sym)
        wrangler = BarDataWranglerV2(
            bar_type=_bar_type(sym),
            price_precision=spec["price_precision"],
            size_precision=spec["size_precision"],
        )
        bars = wrangler.from_pandas(df)
        catalog.write_data([instrument])
        catalog.write_data(bars)
        print(f"{sym:<12} {len(bars):>7,} bars written  ({_bar_type(sym)})")

    print("\n--- Acceptance ---")
    print(f"Instruments: {len(catalog.instruments())}")
    for sym in SYMBOL_SPECS:
        bt = _bar_type(sym)
        try:
            n = len(catalog.bars(bar_types=[bt]))
        except Exception:
            n = 0
        print(f"  {sym:<12} {n:>7,} bars")


if __name__ == "__main__":
    main()
