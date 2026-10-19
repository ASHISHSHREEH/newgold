"""
Short EMA crossover strategy (S1 only) - the mirror of the live long-only EMACross.

This is the control-group short strategy from docs/shortsell_strategy.txt: a
faithful sign-flip of the live long system. It is a SEPARATE module; it does not
import or modify strategy/ema_cross.py.

Entry:  fast EMA crosses BELOW slow EMA (death cross)  -> market SELL (open short).
Exit:   fast EMA crosses ABOVE slow EMA (golden cross) -> close all shorts.

SL/TP (software-managed bar-by-bar, mirror of the long bracket, sign flipped):
  - Initial SL = entry + sl_atr_mult * ATR   (stop ABOVE price)
  - Initial TP = entry - tp_atr_mult * ATR   (target BELOW price)
  - SL HIT:  bar HIGH >= SL  -> close
  - TP HIT:  bar LOW  <= TP  -> close
  - BREAKEVEN: once profit (entry - close) >= breakeven_r * risk, move SL down to entry
  - TRAIL:   after breakeven, trail SL DOWN to (close + risk) whenever that is lower
             (trail only tightens; never moves the stop up/away)

Exits are software-managed (evaluated each bar, closed with a reduce-only MARKET
order), exactly like the live long side. Static SL/TP are also tagged on the
entry order for the dashboard / broker backstop. Pushing the trailing stop to the
venue (via MT5ExecutionClient.modify_position_sltp) is deliberately NOT wired here
- that is the later hybrid step; the control group mirrors live behavior.

Sizing:
  - Default: fixed lots per instrument (trade_sizes), same as the long side.
  - Optional JPY risk sizing (use_risk_sizing=True): lot = risk_jpy /
    (sl_distance * jpy_per_price_per_lot), floored to step, clamped to [min,max],
    skipped if the min lot already exceeds the risk budget. The per-symbol
    jpy_per_price_per_lot / volume bounds are injected via config (precomputed at
    startup with strategy.risk_sizing.probe_symbol_sizing) so the strategy never
    calls mt5 on the event-loop thread.

Limits (same as long): max_per_instrument (default 2), max_total (default 10),
one entry per bar per instrument.
"""

from __future__ import annotations

from datetime import timedelta

from nautilus_trader.indicators.averages import ExponentialMovingAverage
from nautilus_trader.indicators.volatility import AverageTrueRange
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import PositionClosed, PositionOpened
from nautilus_trader.model.identifiers import InstrumentId, PositionId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

from strategy.risk_sizing import SymbolSizing, lot_for_risk

try:
    import monitor_shared as _mon
    _MONITOR = True
except ImportError:
    _MONITOR = False

# Sentinels for a disabled stop/target on the SHORT side (flipped from the long
# side): SL disabled sits at +inf (HIGH never reaches it); TP disabled sits at
# -inf (LOW never reaches it).
_SL_DISABLED = 1e18
_TP_DISABLED = -1e18


class ShortEMACrossConfig(StrategyConfig, frozen=True):
    instruments: tuple[str, ...] = ("GOLD.FXPRO",)
    bar_spec: str = "15-MINUTE-LAST-EXTERNAL"
    fast_ema_period: int = 20
    slow_ema_period: int = 50
    atr_period: int = 14
    # Fixed lot size per instrument, e.g. {"GOLD.FXPRO": "0.01"} (fallback sizing)
    trade_sizes: dict[str, str] = {}
    warmup_bars: int = 200
    max_per_instrument: int = 2
    max_total: int = 10
    enter_on_trend: bool = False  # spec recommends OFF for shorts
    # ATR multiples for SL and TP (0.0 = disabled). Spec notes shorts may want a
    # wider stop (short squeeze), e.g. sl_atr_mult=2.0.
    sl_atr_mult: float = 1.5
    tp_atr_mult: float = 3.0   # ~1:2 risk-reward with sl=1.5
    breakeven_r: float = 1.0
    trail_sl: bool = True

    # --- Optional JPY risk sizing (see strategy.risk_sizing) ---
    use_risk_sizing: bool = False
    risk_jpy_per_trade: float = 0.0
    # Per-instrument constants (keyed by instrument id string), precomputed at
    # startup with probe_symbol_sizing(). Empty -> fall back to trade_sizes.
    jpy_per_price_per_lot: dict[str, float] = {}
    volume_min: dict[str, float] = {}
    volume_step: dict[str, float] = {}
    volume_max: dict[str, float] = {}


class ShortEMACross(Strategy):
    def __init__(self, config: ShortEMACrossConfig | None = None) -> None:
        super().__init__(config or ShortEMACrossConfig())

        self._instrument_ids: list[InstrumentId] = [
            InstrumentId.from_str(s) for s in self.config.instruments
        ]

        self._fast_emas: dict[InstrumentId, ExponentialMovingAverage] = {}
        self._slow_emas: dict[InstrumentId, ExponentialMovingAverage] = {}
        self._atrs: dict[InstrumentId, AverageTrueRange] = {}
        self._fast_prev: dict[InstrumentId, float] = {}
        self._slow_prev: dict[InstrumentId, float] = {}
        self._warmup_done: dict[InstrumentId, bool] = {}
        self._trade_sizes: dict[InstrumentId, Quantity] = {}
        self._sizing: dict[InstrumentId, SymbolSizing] = {}

        # Per-position tracking (same shape as the long side)
        self._pos_entry: dict[PositionId, float] = {}
        self._pos_sl: dict[PositionId, float] = {}
        self._pos_tp: dict[PositionId, float] = {}
        self._pos_risk: dict[PositionId, float] = {}
        self._pos_mfe: dict[PositionId, float] = {}
        self._pos_mae: dict[PositionId, float] = {}
        self._pending_close: set[PositionId] = set()

        for iid in self._instrument_ids:
            self._fast_emas[iid] = ExponentialMovingAverage(self.config.fast_ema_period)
            self._slow_emas[iid] = ExponentialMovingAverage(self.config.slow_ema_period)
            self._atrs[iid] = AverageTrueRange(self.config.atr_period)
            self._fast_prev[iid] = 0.0
            self._slow_prev[iid] = 0.0
            self._warmup_done[iid] = False
            size_str = self.config.trade_sizes.get(str(iid), "0.01")
            self._trade_sizes[iid] = Quantity.from_str(size_str)

            # Build a SymbolSizing spec if risk-sizing constants were provided.
            key = str(iid)
            if self.config.use_risk_sizing and key in self.config.jpy_per_price_per_lot:
                self._sizing[iid] = SymbolSizing(
                    symbol=key,
                    jpy_per_price_per_lot=float(self.config.jpy_per_price_per_lot[key]),
                    volume_min=float(self.config.volume_min.get(key, 0.01)),
                    volume_max=float(self.config.volume_max.get(key, 1e9)),
                    volume_step=float(self.config.volume_step.get(key, 0.01)),
                    digits=2,
                )

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def on_start(self) -> None:
        for iid in self._instrument_ids:
            bar_type = BarType.from_str(f"{iid}-{self.config.bar_spec}")

            self.register_indicator_for_bars(bar_type, self._fast_emas[iid])
            self.register_indicator_for_bars(bar_type, self._slow_emas[iid])
            self.register_indicator_for_bars(bar_type, self._atrs[iid])
            self.subscribe_bars(bar_type)

            hours_back = int(self.config.warmup_bars * 1.5)
            warmup_start = self.clock.utc_now() - timedelta(hours=hours_back)

            def _make_cb(bound_iid: InstrumentId = iid):
                def _cb(req_id) -> None:
                    self._on_warmup_done(bound_iid)
                return _cb

            self.request_bars(
                bar_type,
                start=warmup_start,
                limit=self.config.warmup_bars,
                callback=_make_cb(),
            )

    def on_stop(self) -> None:
        for iid in self._instrument_ids:
            self._close_all(iid, "SHUTDOWN")
            self.cancel_all_orders(iid)

    # -------------------------------------------------------------------------
    # Position events
    # -------------------------------------------------------------------------

    def on_position_opened(self, event: PositionOpened) -> None:
        iid = event.instrument_id
        if iid not in self._atrs:
            return
        atr = self._atrs[iid].value
        if atr <= 0:
            return

        entry = event.avg_px_open
        risk = self.config.sl_atr_mult * atr if self.config.sl_atr_mult > 0 else 0.0
        pid = event.position_id

        self._pos_entry[pid] = entry
        # SHORT: stop ABOVE entry, target BELOW entry
        self._pos_sl[pid] = (entry + risk) if risk > 0 else _SL_DISABLED
        self._pos_tp[pid] = (
            (entry - self.config.tp_atr_mult * atr)
            if self.config.tp_atr_mult > 0 else _TP_DISABLED
        )
        self._pos_risk[pid] = risk
        self._pos_mfe[pid] = 0.0
        self._pos_mae[pid] = 0.0

        if _MONITOR:
            sl_val = self._pos_sl[pid] if self._pos_sl[pid] < 1e17 else None
            tp_val = self._pos_tp[pid] if self._pos_tp[pid] > -1e17 else None
            _mon.update_pos_sltp(str(pid), sl_val, tp_val)
            _mon.record_entry_features(str(pid), {
                "fast_ema": self._fast_emas[iid].value,
                "slow_ema": self._slow_emas[iid].value,
                "atr": atr,
            })

        self._log.info(
            f"TRACK SHORT {pid}  entry={entry:.2f}"
            f"  sl={self._pos_sl[pid]:.2f}"
            f"  tp={self._pos_tp[pid]:.2f}"
            f"  risk={risk:.2f}"
        )

    def on_position_closed(self, event: PositionClosed) -> None:
        pid = event.position_id
        self._pending_close.discard(pid)
        self._pos_entry.pop(pid, None)
        self._pos_sl.pop(pid, None)
        self._pos_tp.pop(pid, None)
        self._pos_risk.pop(pid, None)
        self._pos_mfe.pop(pid, None)
        self._pos_mae.pop(pid, None)

    # -------------------------------------------------------------------------
    # Warmup callback
    # -------------------------------------------------------------------------

    def _on_warmup_done(self, iid: InstrumentId) -> None:
        fast = self._fast_emas[iid]
        slow = self._slow_emas[iid]
        if fast.initialized and slow.initialized:
            self._fast_prev[iid] = fast.value
            self._slow_prev[iid] = slow.value
        self._warmup_done[iid] = True
        self._log.info(
            f"Warmup done {iid} -- fast={fast.value:.2f}"
            f"  slow={slow.value:.2f}  atr={self._atrs[iid].value:.2f}"
        )
        # enter_on_trend (short): if already in a downtrend at warmup, fake a
        # fresh death cross so we enter short on the next bar.
        if self.config.enter_on_trend and fast.value < slow.value:
            self._log.info(f"enter_on_trend: fast < slow for {iid}, faking death cross")
            self._fast_prev[iid] = slow.value + 0.01
            self._slow_prev[iid] = slow.value

    # -------------------------------------------------------------------------
    # Bar handler
    # -------------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        iid = bar.bar_type.instrument_id

        if not self._warmup_done.get(iid, False):
            return

        fast = self._fast_emas[iid]
        slow = self._slow_emas[iid]
        if not (fast.initialized and slow.initialized):
            return

        self._manage_positions(bar, iid)

        fast_val = fast.value
        slow_val = slow.value
        fast_prev = self._fast_prev[iid]
        slow_prev = self._slow_prev[iid]

        self._log.info(
            f"BAR {iid} close={bar.close}"
            f"  fast={fast_val:.2f}  slow={slow_val:.2f}"
        )

        # DEATH CROSS -> open short ; GOLDEN CROSS -> close all shorts
        if fast_prev >= slow_prev and fast_val < slow_val:
            self._open_short(bar, iid)
        elif fast_prev <= slow_prev and fast_val > slow_val:
            self._close_all(iid)

        self._fast_prev[iid] = fast_val
        self._slow_prev[iid] = slow_val

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _clear_pos(self, pid: PositionId, reason: str = "CLOSED") -> None:
        self._pending_close.add(pid)
        self._pos_entry.pop(pid, None)
        self._pos_sl.pop(pid, None)
        self._pos_tp.pop(pid, None)
        self._pos_risk.pop(pid, None)
        if _MONITOR:
            _mon.record_close_reason(str(pid), reason)

    # -------------------------------------------------------------------------
    # Trailing-stop / SL-TP manager (called every bar) - SHORT side
    # -------------------------------------------------------------------------

    def _manage_positions(self, bar: Bar, iid: InstrumentId) -> None:
        low = float(bar.low)
        high = float(bar.high)
        close = float(bar.close)

        for pos in list(self.cache.positions_open(instrument_id=iid, strategy_id=self.id)):
            pid = pos.id
            if pid in self._pending_close:
                continue
            if pid not in self._pos_sl:
                continue

            sl = self._pos_sl[pid]
            tp = self._pos_tp[pid]
            entry = self._pos_entry[pid]
            risk = self._pos_risk[pid]

            # MAE/MFE (short: favorable = entry - low, adverse = high - entry)
            fav = entry - low
            adv = high - entry
            if fav > self._pos_mfe.get(pid, 0.0):
                self._pos_mfe[pid] = fav
            if adv > self._pos_mae.get(pid, 0.0):
                self._pos_mae[pid] = adv
            if _MONITOR:
                _mon.update_excursion(str(pid), self._pos_mae[pid], self._pos_mfe[pid])

            # TP hit (short): LOW <= TP
            if low <= tp:
                self._clear_pos(pid, "TP")
                self.close_position(pos)
                self._log.info(f"TP HIT {pid}  tp={tp:.2f}  low={low:.2f}")
                continue

            # SL hit (short): HIGH >= SL
            if high >= sl:
                self._clear_pos(pid, "SL")
                self.close_position(pos)
                self._log.info(f"SL HIT {pid}  sl={sl:.2f}  high={high:.2f}")
                continue

            # Move to breakeven once profit (entry - close) >= breakeven_r * risk
            if risk > 0 and sl > entry:
                profit = entry - close
                if profit >= self.config.breakeven_r * risk:
                    self._pos_sl[pid] = entry
                    if _MONITOR:
                        _mon.update_pos_sltp(str(pid), entry, tp if tp > -1e17 else None)
                    self._log.info(
                        f"SL -> BREAKEVEN {pid}  entry={entry:.2f}  profit={profit:.2f}"
                    )

            # Trail after breakeven (short): tighten SL DOWN to close + risk
            if self.config.trail_sl and risk > 0 and self._pos_sl[pid] <= entry:
                trail = close + risk
                if trail < self._pos_sl[pid]:
                    self._pos_sl[pid] = trail
                    if _MONITOR:
                        _mon.update_pos_sltp(str(pid), trail, tp if tp > -1e17 else None)
                    self._log.info(
                        f"SL TRAIL {pid}  sl={trail:.2f}  close={close:.2f}"
                    )

    # -------------------------------------------------------------------------
    # Order helpers
    # -------------------------------------------------------------------------

    def _qty_for(self, iid: InstrumentId, close: float, atr: float) -> Quantity | None:
        """
        Resolve the order quantity. Uses JPY risk sizing when configured and a
        SymbolSizing spec is available, otherwise the fixed per-instrument lot.
        Returns None when risk sizing says the trade should be skipped.
        """
        instrument = self.cache.instrument(iid)
        size_prec = instrument.size_precision if instrument else 2

        spec = self._sizing.get(iid)
        if spec is not None and self.config.risk_jpy_per_trade > 0 and atr > 0:
            sl_distance = self.config.sl_atr_mult * atr
            lots = lot_for_risk(self.config.risk_jpy_per_trade, sl_distance, spec)
            if lots <= 0.0:
                self._log.info(
                    f"SKIP {iid}: risk sizing -> 0 lots "
                    f"(min lot risks > {self.config.risk_jpy_per_trade:.0f} JPY)"
                )
                return None
            return Quantity(lots, size_prec)

        return self._trade_sizes[iid]

    def _open_short(self, bar: Bar, iid: InstrumentId) -> None:
        open_orders = len(self.cache.orders_open(instrument_id=iid, strategy_id=self.id))
        open_positions = len(self.cache.positions_open(instrument_id=iid, strategy_id=self.id))
        per_instrument = open_orders + open_positions
        if per_instrument >= self.config.max_per_instrument:
            self._log.info(
                f"SKIP {iid}: already at max_per_instrument={self.config.max_per_instrument}"
            )
            return

        total = sum(
            len(self.cache.positions_open(instrument_id=i, strategy_id=self.id))
            + len(self.cache.orders_open(instrument_id=i, strategy_id=self.id))
            for i in self._instrument_ids
        )
        if total >= self.config.max_total:
            self._log.info(
                f"SKIP {iid}: total positions={total} >= max_total={self.config.max_total}"
            )
            return

        atr = self._atrs[iid].value
        close = float(bar.close)
        # SHORT bracket: SL above, TP below
        sl_approx = close + self.config.sl_atr_mult * atr
        tp_approx = close - self.config.tp_atr_mult * atr

        instrument = self.cache.instrument(iid)
        prec = instrument.price_precision if instrument else 2

        qty = self._qty_for(iid, close, atr)
        if qty is None:
            return

        tags: list[str] = []
        if self.config.sl_atr_mult > 0:
            tags.append(f"SL:{sl_approx:.{prec}f}")
        if self.config.tp_atr_mult > 0:
            tags.append(f"TP:{tp_approx:.{prec}f}")

        order = self.order_factory.market(
            instrument_id=iid,
            order_side=OrderSide.SELL,
            quantity=qty,
            tags=tags or None,
        )
        self.submit_order(order)
        self._log.info(
            f"SHORT {iid}  bar={bar.close}  qty={qty}"
            f"  fast={self._fast_emas[iid].value:.2f}"
            f"  slow={self._slow_emas[iid].value:.2f}"
            f"  atr={atr:.2f}"
            f"  sl={sl_approx:.{prec}f}  tp={tp_approx:.{prec}f}"
            f"  [pos: {per_instrument+1}/{self.config.max_per_instrument}"
            f"  total: {total+1}/{self.config.max_total}]"
        )

    def _close_all(self, iid: InstrumentId, reason: str = "EMA") -> None:
        for pos in self.cache.positions_open(instrument_id=iid, strategy_id=self.id):
            if pos.id in self._pending_close:
                continue
            self._clear_pos(pos.id, reason)
            self.close_position(pos)
            self._log.info(
                f"CLOSE {iid}  pos={pos.id}"
                f"  fast={self._fast_emas[iid].value:.2f}"
                f"  slow={self._slow_emas[iid].value:.2f}"
            )
