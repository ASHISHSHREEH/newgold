"""
EMA crossover strategy — multiple instruments, position limits per instrument and globally.

Entry:  fast EMA crosses above slow EMA (golden cross) → market BUY.
Exit:   fast EMA crosses below slow EMA (death cross)  → close all longs for that instrument.

SL/TP (managed bar-by-bar internally):
  - Initial SL = entry - sl_atr_mult * ATR
  - Initial TP = entry + tp_atr_mult * ATR  (default 4.0 → 2:1 ratio with sl=2.0)
  - Once profit >= breakeven_r * initial_risk → SL moves to entry (breakeven, no loss possible)
  - After breakeven, SL trails at (close - initial_risk) whenever that is higher than current SL

Limits
------
max_per_instrument : max open positions per symbol (default 2)
max_total          : max total open positions across all symbols (default 10)
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

try:
    import monitor_shared as _mon
    _MONITOR = True
except ImportError:
    _MONITOR = False


class EMACrossConfig(StrategyConfig, frozen=True):
    # Instrument IDs as strings, e.g. ["GOLD.FXPRO", "NAS100.FXPRO"]
    instruments: tuple[str, ...] = ("GOLD.FXPRO",)
    # Bar spec shared across all instruments: "1-HOUR-LAST-EXTERNAL"
    bar_spec: str = "1-HOUR-LAST-EXTERNAL"
    fast_ema_period: int = 20
    slow_ema_period: int = 50
    atr_period: int = 14
    # Lot size per instrument, e.g. {"GOLD.FXPRO": "0.01"}
    trade_sizes: dict[str, str] = {}
    warmup_bars: int = 200
    max_per_instrument: int = 2
    max_total: int = 10
    enter_on_trend: bool = False
    # ATR multiples for SL and TP (0.0 = disabled)
    sl_atr_mult: float = 2.0
    tp_atr_mult: float = 4.0   # 2:1 risk-reward with sl=2.0
    # Move SL to entry (breakeven) once profit reaches this multiple of initial risk
    breakeven_r: float = 1.0
    # After breakeven, trail SL at (close - initial_risk) whenever that is higher
    trail_sl: bool = True


class EMACross(Strategy):
    def __init__(self, config: EMACrossConfig | None = None) -> None:
        super().__init__(config or EMACrossConfig())

        self._instrument_ids: list[InstrumentId] = [
            InstrumentId.from_str(s) for s in self.config.instruments
        ]

        # Per-instrument indicator state
        self._fast_emas: dict[InstrumentId, ExponentialMovingAverage] = {}
        self._slow_emas: dict[InstrumentId, ExponentialMovingAverage] = {}
        self._atrs: dict[InstrumentId, AverageTrueRange] = {}
        self._fast_prev: dict[InstrumentId, float] = {}
        self._slow_prev: dict[InstrumentId, float] = {}
        self._warmup_done: dict[InstrumentId, bool] = {}
        self._trade_sizes: dict[InstrumentId, Quantity] = {}

        # Per-position trailing-stop state
        self._pos_entry: dict[PositionId, float] = {}
        self._pos_sl: dict[PositionId, float] = {}
        self._pos_tp: dict[PositionId, float] = {}
        self._pos_risk: dict[PositionId, float] = {}  # initial SL distance in price
        # Max favorable / adverse excursion in price points over the trade's life
        # (captured for future ML training, written to the trades DB on close).
        self._pos_mfe: dict[PositionId, float] = {}
        self._pos_mae: dict[PositionId, float] = {}
        # Positions for which close has been submitted but fill not yet confirmed.
        # Prevents double-close within the same bar and across bars while pending.
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
    # Position events — set up and tear down trailing-stop state
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
        self._pos_sl[pid] = (entry - risk) if risk > 0 else -1e18
        self._pos_tp[pid] = (entry + self.config.tp_atr_mult * atr) if self.config.tp_atr_mult > 0 else 1e18
        self._pos_risk[pid] = risk
        self._pos_mfe[pid] = 0.0
        self._pos_mae[pid] = 0.0

        if _MONITOR:
            sl_val = self._pos_sl[pid] if self._pos_sl[pid] > -1e17 else None
            tp_val = self._pos_tp[pid] if self._pos_tp[pid] < 1e17 else None
            _mon.update_pos_sltp(str(pid), sl_val, tp_val)
            # Entry-time indicator snapshot — the feature vector for ML training.
            _mon.record_entry_features(str(pid), {
                "fast_ema": self._fast_emas[iid].value,
                "slow_ema": self._slow_emas[iid].value,
                "atr": atr,
            })

        self._log.info(
            f"TRACK {pid}  entry={entry:.2f}"
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
        if self.config.enter_on_trend and fast.value > slow.value:
            self._log.info(f"enter_on_trend: fast > slow for {iid}, faking golden cross")
            self._fast_prev[iid] = slow.value - 0.01
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

        # Manage SL/TP and trailing stops for all open positions first
        self._manage_positions(bar, iid)

        fast_val = fast.value
        slow_val = slow.value
        fast_prev = self._fast_prev[iid]
        slow_prev = self._slow_prev[iid]

        self._log.info(
            f"BAR {iid} close={bar.close}"
            f"  fast={fast_val:.2f}  slow={slow_val:.2f}"
        )

        if fast_prev <= slow_prev and fast_val > slow_val:
            self._open_long(bar, iid)
        elif fast_prev >= slow_prev and fast_val < slow_val:
            self._close_all(iid)

        self._fast_prev[iid] = fast_val
        self._slow_prev[iid] = slow_val

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _clear_pos(self, pid: PositionId, reason: str = "CLOSED") -> None:
        """Mark a position as pending-close and remove it from all tracking dicts."""
        self._pending_close.add(pid)
        self._pos_entry.pop(pid, None)
        self._pos_sl.pop(pid, None)
        self._pos_tp.pop(pid, None)
        self._pos_risk.pop(pid, None)
        if _MONITOR:
            _mon.record_close_reason(str(pid), reason)

    # -------------------------------------------------------------------------
    # Trailing-stop / SL-TP manager (called every bar)
    # -------------------------------------------------------------------------

    def _manage_positions(self, bar: Bar, iid: InstrumentId) -> None:
        low = float(bar.low)
        high = float(bar.high)
        close = float(bar.close)

        for pos in list(self.cache.positions_open(instrument_id=iid, strategy_id=self.id)):
            pid = pos.id
            # Skip if already closing (submitted but fill not yet confirmed)
            if pid in self._pending_close:
                continue
            if pid not in self._pos_sl:
                continue

            sl = self._pos_sl[pid]
            tp = self._pos_tp[pid]
            entry = self._pos_entry[pid]
            risk = self._pos_risk[pid]

            # ── Track MAE/MFE (long-only: favorable = high-entry, adverse = entry-low) ──
            fav = high - entry
            adv = entry - low
            if fav > self._pos_mfe.get(pid, 0.0):
                self._pos_mfe[pid] = fav
            if adv > self._pos_mae.get(pid, 0.0):
                self._pos_mae[pid] = adv
            if _MONITOR:
                _mon.update_excursion(str(pid), self._pos_mae[pid], self._pos_mfe[pid])

            # ── TP hit ──────────────────────────────────────────────────────
            if high >= tp:
                self._clear_pos(pid, "TP")
                self.close_position(pos)
                self._log.info(f"TP HIT {pid}  tp={tp:.2f}  high={high:.2f}")
                continue

            # ── SL hit ──────────────────────────────────────────────────────
            if low <= sl:
                self._clear_pos(pid, "SL")
                self.close_position(pos)
                self._log.info(f"SL HIT {pid}  sl={sl:.2f}  low={low:.2f}")
                continue

            # ── Move to breakeven once profit >= breakeven_r × risk ─────────
            if risk > 0 and sl < entry:
                profit = close - entry
                if profit >= self.config.breakeven_r * risk:
                    self._pos_sl[pid] = entry
                    if _MONITOR:
                        _mon.update_pos_sltp(str(pid), entry, tp if tp < 1e17 else None)
                    self._log.info(
                        f"SL → BREAKEVEN {pid}"
                        f"  entry={entry:.2f}  profit={profit:.2f}"
                    )

            # ── Trail after breakeven ────────────────────────────────────────
            if self.config.trail_sl and risk > 0 and self._pos_sl[pid] >= entry:
                trail = close - risk
                if trail > self._pos_sl[pid]:
                    self._pos_sl[pid] = trail
                    if _MONITOR:
                        _mon.update_pos_sltp(str(pid), trail, tp if tp < 1e17 else None)
                    self._log.info(
                        f"SL TRAIL {pid}  sl={trail:.2f}  close={close:.2f}"
                    )

    # -------------------------------------------------------------------------
    # Order helpers
    # -------------------------------------------------------------------------

    def _open_long(self, bar: Bar, iid: InstrumentId) -> None:
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
        sl_approx = close - self.config.sl_atr_mult * atr
        tp_approx = close + self.config.tp_atr_mult * atr

        # Use instrument price precision so MT5 doesn't reject the SL/TP values.
        instrument = self.cache.instrument(iid)
        prec = instrument.price_precision if instrument else 2

        tags: list[str] = []
        if self.config.sl_atr_mult > 0:
            tags.append(f"SL:{sl_approx:.{prec}f}")
        if self.config.tp_atr_mult > 0:
            tags.append(f"TP:{tp_approx:.{prec}f}")

        order = self.order_factory.market(
            instrument_id=iid,
            order_side=OrderSide.BUY,
            quantity=self._trade_sizes[iid],
            tags=tags or None,
        )
        self.submit_order(order)
        self._log.info(
            f"LONG  {iid}  bar={bar.close}"
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
