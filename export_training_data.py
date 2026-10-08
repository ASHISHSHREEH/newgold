"""
Export a clean ML training table from the live trading data.

Sources (both written by MonitorActor):
  data/trading_mt5.db   — `trades` table: entry features + outcomes per trade
  data/bars/*.parquet   — every bar received, for pre-entry market context

Output:
  data/training_data.csv      (human-readable)
  data/training_data.parquet  (typed, for pandas/ML)

Each row = one CLOSED trade with:
  Features (known at entry): fast_ema, slow_ema, atr_entry, ema_gap, ema_gap_atr,
    hour, day_of_week, + pre-entry returns ret_1/ret_4/ret_8 (if bars available)
  Labels (known at exit): profit, is_win, ret, ret_r (R-multiple), mae, mfe,
    mae_r, mfe_r, duration_min, exit_reason

Run:
    .venv\\Scripts\\python export_training_data.py
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

_DATA_DIR = Path(__file__).parent / "data"
_DB_PATH = _DATA_DIR / "trading_mt5.db"
_BARS_DIR = _DATA_DIR / "bars"
_OUT_CSV = _DATA_DIR / "training_data.csv"
_OUT_PARQUET = _DATA_DIR / "training_data.parquet"


def load_trades() -> pd.DataFrame:
    """Load closed trades from SQLite."""
    if not _DB_PATH.exists():
        raise FileNotFoundError(f"No trades DB at {_DB_PATH}")
    conn = sqlite3.connect(str(_DB_PATH))
    try:
        df = pd.read_sql_query("SELECT * FROM trades", conn)
    finally:
        conn.close()
    # Only closed trades have labels.
    df = df[df["close_time"].notna()].copy()
    return df


def load_bars() -> pd.DataFrame | None:
    """Load all archived bars, with pre-computed pre-entry returns per symbol."""
    if not _BARS_DIR.exists() or not any(_BARS_DIR.glob("*.parquet")):
        return None
    bars = pd.read_parquet(_BARS_DIR)
    if bars.empty:
        return None
    bars["dt"] = pd.to_datetime(bars["ts_event"], utc=True)
    # De-dup (restarts re-request overlapping warmup bars) and order per symbol.
    bars = (
        bars.drop_duplicates(subset=["symbol", "ts_event_ns"])
        .sort_values(["symbol", "dt"])
        .reset_index(drop=True)
    )
    close = bars.groupby("symbol")["close"]
    bars["ret_1"] = close.pct_change(1)   # last bar's return
    bars["ret_4"] = close.pct_change(4)   # ~1h on M15
    bars["ret_8"] = close.pct_change(8)   # ~2h on M15
    return bars


def add_trade_features(df: pd.DataFrame) -> pd.DataFrame:
    """Derive features/labels computable from the trade record alone."""
    df = df.copy()
    open_dt = pd.to_datetime(df["open_time"], utc=True, errors="coerce")
    df["open_dt"] = open_dt
    df["hour"] = open_dt.dt.hour
    df["day_of_week"] = open_dt.dt.dayofweek

    # Signal strength at entry.
    df["ema_gap"] = df["fast_ema"] - df["slow_ema"]
    df["ema_gap_atr"] = df["ema_gap"] / df["atr_entry"].where(df["atr_entry"] > 0)

    # Risk in price points (long-only: entry - stop). Used to normalize outcomes.
    risk = df["entry_price"] - df["sl"]
    risk = risk.where(risk > 0)
    df["risk"] = risk

    # Labels.
    df["is_win"] = (df["profit"] > 0).astype("Int64")
    df["ret"] = (df["exit_price"] - df["entry_price"]) / df["entry_price"]
    df["ret_r"] = (df["exit_price"] - df["entry_price"]) / risk      # realized R-multiple
    df["mae_r"] = df["mae"] / risk                                  # worst drawdown in R
    df["mfe_r"] = df["mfe"] / risk                                  # best excursion in R
    df["duration_min"] = df["duration_sec"] / 60.0
    return df


def join_bar_context(trades: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    """Attach the most recent bar at/before each trade's entry (merge_asof)."""
    cols = ["symbol", "dt", "ret_1", "ret_4", "ret_8", "close"]
    right = bars[cols].rename(columns={"close": "bar_close_at_entry"})
    left = trades.dropna(subset=["open_dt"]).sort_values("open_dt")
    right = right.sort_values("dt")
    merged = pd.merge_asof(
        left, right,
        left_on="open_dt", right_on="dt", by="symbol", direction="backward",
    )
    return merged.drop(columns=["dt"])


def main() -> None:
    trades = load_trades()
    if trades.empty:
        print("No closed trades yet — nothing to export. Let the bot run and take some trades first.")
        return

    df = add_trade_features(trades)

    bars = load_bars()
    if bars is not None:
        df = join_bar_context(df, bars)
        print(f"Joined pre-entry bar context from {len(bars):,} archived bars.")
    else:
        print("No bar archive found - exporting trade-only features (ret_1/4/8 omitted).")

    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(_OUT_CSV, index=False)
    df.to_parquet(_OUT_PARQUET, index=False)

    n = len(df)
    wins = int(df["is_win"].sum()) if n else 0
    print(f"\nExported {n} closed trades")
    print(f"  win rate : {wins}/{n} ({wins / n:.1%})" if n else "  win rate : n/a")
    if df["ret_r"].notna().any():
        print(f"  mean R   : {df['ret_r'].mean():.2f}")
    print(f"  -> {_OUT_CSV}")
    print(f"  -> {_OUT_PARQUET}")


if __name__ == "__main__":
    main()
