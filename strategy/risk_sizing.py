"""
JPY risk-based position sizing for the Hybrid Short Strategy.

Why this exists
---------------
The account is JPY; the instruments are USD-denominated with different contract
sizes (GOLD x100, WTI x1000, indices/BTC x1). MT5's order_calc_profit() returns
the TRUE JPY P&L for a given volume and price move and folds in the USD->JPY
conversion, so it is the single source of truth for both sizing and P&L. Using
it fixes the long-standing mismatch where Nautilus reported a tiny P&L versus the
real JPY account move (off by each instrument's contract multiplier).

Verified on FxPro demo (2026-10-19) that order_calc_profit is LINEAR in both
volume and price move for all 6 live instruments. So we precompute ONE constant
per symbol at startup -- "JPY per 1.0 price unit per 1.0 lot" -- and size with
pure arithmetic at signal time (no mt5 call, no worker-thread hop):

    jpy_per_price_per_lot ~= trade_contract_size * USDJPY_rate
    GOLD=15809  WTI=158091  #USNDAQ100/#USSPX500/BITCOIN/#Japan225=158

Caveat: the constant embeds the USDJPY rate at the moment it was probed. The rate
drifts intraday (well under ~1%), so refresh it periodically (e.g. daily, or on
reconnect) if you want sizing accuracy tighter than that.

Sizing
------
    loss_per_lot_at_stop = stop_distance_price * jpy_per_price_per_lot
    volume               = risk_jpy / loss_per_lot_at_stop
then floored to volume_step and clamped to [volume_min, volume_max].

The pure functions (floor_to_step, lot_for_risk, expected_loss_jpy) import
nothing from MT5 and are fully unit-testable offline. Only probe_symbol_sizing()
touches mt5 and must run at startup or inside the exec worker thread.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import MetaTrader5 as mt5


@dataclass(frozen=True)
class SymbolSizing:
    """Precomputed per-symbol sizing constants (snapshot at probe time)."""

    symbol: str
    jpy_per_price_per_lot: float  # JPY P&L for a 1.0 price move on a 1.0 lot
    volume_min: float
    volume_max: float
    volume_step: float
    digits: int


# --------------------------------------------------------------------------- #
# Pure math (no MT5) -- unit-testable offline
# --------------------------------------------------------------------------- #
def _step_decimals(step: float) -> int:
    """Number of decimal places implied by a volume step (e.g. 0.01 -> 2)."""
    if step <= 0:
        return 2
    s = f"{step:.10f}".rstrip("0").rstrip(".")
    return len(s.split(".")[1]) if "." in s else 0


def floor_to_step(volume: float, step: float) -> float:
    """Floor a volume down to the nearest multiple of step."""
    if step <= 0:
        return volume
    # small epsilon so values already on-step don't drop a whole step due to FP
    return math.floor(volume / step + 1e-9) * step


def lot_for_risk(
    risk_jpy: float,
    stop_distance_price: float,
    spec: SymbolSizing,
) -> float:
    """
    Lot size whose loss at the stop is approximately risk_jpy, floored to the
    volume step and clamped to [volume_min, volume_max].

    Returns 0.0 when the trade should be SKIPPED because even volume_min would
    risk more than risk_jpy. When the result is clamped UP to volume_max, the
    realized risk may exceed risk_jpy -- check expected_loss_jpy() if that matters.
    """
    if risk_jpy <= 0 or stop_distance_price <= 0 or spec.jpy_per_price_per_lot <= 0:
        return 0.0
    loss_per_lot = stop_distance_price * spec.jpy_per_price_per_lot
    raw = risk_jpy / loss_per_lot
    vol = floor_to_step(raw, spec.volume_step)
    if vol < spec.volume_min:
        return 0.0  # smallest tradable lot already exceeds the risk budget
    if vol > spec.volume_max:
        vol = spec.volume_max
    return round(vol, _step_decimals(spec.volume_step))


def expected_loss_jpy(
    volume: float,
    stop_distance_price: float,
    spec: SymbolSizing,
) -> float:
    """JPY loss if a position of `volume` lots is stopped out at stop_distance_price."""
    return volume * stop_distance_price * spec.jpy_per_price_per_lot


# --------------------------------------------------------------------------- #
# MT5-touching probe -- run at startup or inside the exec worker thread
# --------------------------------------------------------------------------- #
def probe_symbol_sizing(symbol: str) -> SymbolSizing | None:
    """
    Compute the per-lot JPY/price constant from order_calc_profit at the current
    price. Returns None if the symbol is unavailable or the calc fails.

    Threading: calls mt5.* -- run it at startup or in the exec worker thread,
    never concurrently with other mt5 calls from another thread.
    """
    if not mt5.symbol_select(symbol, True):
        return None
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if info is None or tick is None or tick.ask == 0:
        return None
    price = tick.ask
    jpp = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, 1.0, price, price + 1.0)
    if jpp is None or jpp <= 0:
        return None
    return SymbolSizing(
        symbol=symbol,
        jpy_per_price_per_lot=float(jpp),
        volume_min=float(info.volume_min),
        volume_max=float(info.volume_max),
        volume_step=float(info.volume_step),
        digits=int(info.digits),
    )
