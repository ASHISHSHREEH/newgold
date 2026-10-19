"""
Multi-detector SHORT strategy (S1 + S2 + S4) on one shared engine.

Design (docs/shortsell_strategy.txt + agreed build order): ONE shared engine
- regime filter, ATR risk bracket, software-managed exits, JPY risk sizing and
position limits - with pluggable entry DETECTORS. Each detector only decides
"short here?"; everything else is shared. Entries are tagged with the detector
id (DET:S1 / DET:S2 / DET:S4) so a backtest can attribute performance per
detector and judge each against S1.

This is a SEPARATE module. It does not modify strategy/ema_cross.py (live long)
or strategy/short_ema_cross.py (the S1-only control). Running this with
detectors=("S1",) reproduces ShortEMACross as a cross-check.

Detectors (confirmed 2026-10-19)
--------------------------------
S1  EMA death cross:
      prev fast >= slow AND this fast < slow            -> short
S2  EMA pullback / resumption (continuation):
      regime fast < slow (downtrend), AND a prior bar's HIGH reached the slow-EMA
      zone (high >= slow_ema - pullback_atr*ATR), THEN a later bar CLOSES below
      the fast EMA                                       -> short
S4  Donchian lower-band breakdown, downtrend-gated:
      regime fast < slow, AND close < min(low) of the prior donchian_period bars
                                                         -> short

Shared regime EXIT: a golden cross (fast crosses back above slow) closes all
shorts for that instrument, regardless of which detector opened them.

Stops/targets/sizing/limits: identical to ShortEMACross (see that module).
"""

from __future__ import annotations

from collections import deque
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

_SL_DISABLED = 1e18
_TP_DISABLED = -1e18

_VALID_DETECTORS = ("S1", "S2", "S4")


class MultiShortConfig(StrategyConfig, frozen=True):
    instruments: tuple[str, ...] = ("GOLD.FXPRO",)
    bar_spec: str = "15-MINUTE-LAST-EXTERNAL"
    fast_ema_period: int = 20
    slow_ema_period: int = 50
    atr_period: int = 14
    trade_sizes: dict[str, str] = {}
    warmup_bars: int = 200
    max_per_instrument: int = 2
    max_total: int = 10

    # Which detectors are active, evaluated in this order (first to fire wins the bar).
    detectors: tuple[str, ...] = ("S1", "S2", "S4")

    # Stops / targets (short side: SL above, TP below)
    sl_atr_mult: float = 1.5
    tp_atr_mult: float = 3.0
    breakeven_r: float = 1.0       # move SL to entry after this many R profit (large = disable)
    trail_sl: bool = True
    exit_on_regime_flip: bool = True  # golden cross closes all shorts (set False to isolate)

    # Detector params
    pullback_atr: float = 0.25     # S2: how close the pullback high must get to slow EMA
    donchian_period: int = 20      # S4: lookback for the prior-bar low breakdown

    # Regime / trend-strength filter (gates ALL detectors). Both OFF by default
    # so the unfiltered behavior is unchanged; turn on to only short established,
    # falling downtrends and stand aside in chop (the 2022 lesson).
    regime_slope_bars: int = 0         # require slow EMA now < slow EMA this many bars ago
    regime_require_below_slow: bool = False  # require close < slow EMA

    # Optional JPY risk sizing (see strategy.risk_sizing)
    use_risk_sizing: bool = False
    risk_jpy_per_trade: float = 0.0
    jpy_per_price_per_lot: dict[str, float] = {}
    volume_min: dict[str, float] = {}
    volume_step: dict[str, float] = {}
    volume_max: dict[str, float] = {}


class MultiShortEMA(Strategy):
    def __init__(self, config: MultiShortConfig | None = None) -> None:
        super().__init__(config or MultiShortConfig())

        bad = [d for d in self.config.detectors if d not in _VALID_DETECTORS]
        if bad:
            raise ValueError(f"unknown detectors {bad}; valid: {_VALID_DETECTORS}")

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

        # Detector state
        self._s2_armed: dict[InstrumentId, bool] = {}
        self._prior_lows: dict[InstrumentId, deque] = {}
        # Regime filter state: history of slow-EMA values for the slope check.
        self._slow_hist: dict[InstrumentId, deque] = {}

        # Per-position tracking
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
            self._s2_armed[iid] = False
            self._prior_lows[iid] = deque(maxlen=self.config.donchian_period)
            self._slow_hist[iid] = deque(maxlen=max(1, self.config.regime_slope_bars))
            size_str = self.config.trade_sizes.get(str(iid), "0.01")
            self._trade_sizes[iid] = Quantity.from_str(size_str)

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
            f"  sl={self._pos_sl[pid]:.2f}  tp={self._pos_tp[pid]:.2f}  risk={risk:.2f}"
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

        # Manage open positions first (SL/TP/breakeven/trail)
        self._manage_positions(bar, iid)

        fast_val = fast.value
        slow_val = slow.value
        fast_prev = self._fast_prev[iid]
        slow_prev = self._slow_prev[iid]
        atr = self._atrs[iid].value
        downtrend = fast_val < slow_val

        # Regime EXIT: golden cross closes all shorts for this instrument.
        golden = fast_prev <= slow_prev and fast_val > slow_val
        if golden and self.config.exit_on_regime_flip:
            self._close_all(iid)

        # Evaluate detectors (first to fire wins the bar), only if not exiting
        # and only if the regime filter allows shorting right now.
        if not golden and self._regime_ok(bar, iid, slow_val):
            fired = self._evaluate_detectors(bar, iid, fast_val, slow_val,
                                             fast_prev, slow_prev, atr, downtrend)
            if fired is not None:
                self._open_short(bar, iid, atr, fired)

        # Update trailing detector state AFTER evaluation.
        # S2 arming: in a downtrend, a bar whose HIGH reached the slow-EMA zone.
        if downtrend and atr > 0 and float(bar.high) >= slow_val - self.config.pullback_atr * atr:
            self._s2_armed[iid] = True
        elif not downtrend:
            self._s2_armed[iid] = False
        # S4 rolling prior-low window (append AFTER the breakdown check used it).
        self._prior_lows[iid].append(float(bar.low))
        # Slow-EMA history for the regime slope check (append AFTER the check).
        if self.config.regime_slope_bars > 0:
            self._slow_hist[iid].append(slow_val)

        self._fast_prev[iid] = fast_val
        self._slow_prev[iid] = slow_val

    def _regime_ok(self, bar: Bar, iid: InstrumentId, slow_val: float) -> bool:
        """
        Trend-strength gate shared by all detectors. Returns True when shorting is
        allowed. Both conditions are opt-in (disabled by default):
          - require_below_slow: price must be below the slow EMA
          - slope_bars > 0:     slow EMA must be falling vs this many bars ago
        """
        if self.config.regime_require_below_slow and float(bar.close) >= slow_val:
            return False
        if self.config.regime_slope_bars > 0:
            hist = self._slow_hist[iid]
            if len(hist) < self.config.regime_slope_bars:
                return False  # not enough history yet -> stand aside
            if not (slow_val < hist[0]):
                return False  # slow EMA not falling -> chop/uptrend, skip
        return True

    # -------------------------------------------------------------------------
    # Detectors
    # -------------------------------------------------------------------------

    def _evaluate_detectors(self, bar, iid, fast_val, slow_val,
                            fast_prev, slow_prev, atr, downtrend) -> str | None:
        """Return the id of the first enabled detector that fires, else None."""
        close = float(bar.close)
        high = float(bar.high)

        for det in self.config.detectors:
            if det == "S1":
                # death cross on this bar
                if fast_prev >= slow_prev and fast_val < slow_val:
                    return "S1"
            elif det == "S2":
                # armed pullback (set on a PRIOR bar) + resumption below fast EMA
                if downtrend and self._s2_armed.get(iid, False) and close < fast_val:
                    self._s2_armed[iid] = False  # consume the arm
                    return "S2"
            elif det == "S4":
                # close below the lowest low of the prior donchian_period bars
                lows = self._prior_lows[iid]
                if downtrend and len(lows) == lows.maxlen:
                    if close < min(lows):
                        return "S4"
        return None

    # -------------------------------------------------------------------------
    # Exit manager (short side) - identical logic to ShortEMACross
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

            fav = entry - low
            adv = high - entry
            if fav > self._pos_mfe.get(pid, 0.0):
                self._pos_mfe[pid] = fav
            if adv > self._pos_mae.get(pid, 0.0):
                self._pos_mae[pid] = adv
            if _MONITOR:
                _mon.update_excursion(str(pid), self._pos_mae[pid], self._pos_mfe[pid])

            if low <= tp:
                self._clear_pos(pid, "TP")
                self.close_position(pos)
                self._log.info(f"TP HIT {pid}  tp={tp:.2f}  low={low:.2f}")
                continue
            if high >= sl:
                self._clear_pos(pid, "SL")
                self.close_position(pos)
                self._log.info(f"SL HIT {pid}  sl={sl:.2f}  high={high:.2f}")
                continue

            if risk > 0 and sl > entry:
                profit = entry - close
                if profit >= self.config.breakeven_r * risk:
                    self._pos_sl[pid] = entry
                    if _MONITOR:
                        _mon.update_pos_sltp(str(pid), entry, tp if tp > -1e17 else None)
                    self._log.info(f"SL -> BREAKEVEN {pid}  entry={entry:.2f}  profit={profit:.2f}")

            if self.config.trail_sl and risk > 0 and self._pos_sl[pid] <= entry:
                trail = close + risk
                if trail < self._pos_sl[pid]:
                    self._pos_sl[pid] = trail
                    if _MONITOR:
                        _mon.update_pos_sltp(str(pid), trail, tp if tp > -1e17 else None)
                    self._log.info(f"SL TRAIL {pid}  sl={trail:.2f}  close={close:.2f}")

    # -------------------------------------------------------------------------
    # Order helpers
    # -------------------------------------------------------------------------

    def _clear_pos(self, pid: PositionId, reason: str = "CLOSED") -> None:
        self._pending_close.add(pid)
        self._pos_entry.pop(pid, None)
        self._pos_sl.pop(pid, None)
        self._pos_tp.pop(pid, None)
        self._pos_risk.pop(pid, None)
        if _MONITOR:
            _mon.record_close_reason(str(pid), reason)

    def _qty_for(self, iid: InstrumentId, close: float, atr: float) -> Quantity | None:
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

    def _open_short(self, bar: Bar, iid: InstrumentId, atr: float, detector: str) -> None:
        open_orders = len(self.cache.orders_open(instrument_id=iid, strategy_id=self.id))
        open_positions = len(self.cache.positions_open(instrument_id=iid, strategy_id=self.id))
        per_instrument = open_orders + open_positions
        if per_instrument >= self.config.max_per_instrument:
            self._log.info(f"SKIP {iid}: at max_per_instrument={self.config.max_per_instrument}")
            return

        total = sum(
            len(self.cache.positions_open(instrument_id=i, strategy_id=self.id))
            + len(self.cache.orders_open(instrument_id=i, strategy_id=self.id))
            for i in self._instrument_ids
        )
        if total >= self.config.max_total:
            self._log.info(f"SKIP {iid}: total={total} >= max_total={self.config.max_total}")
            return

        close = float(bar.close)
        sl_approx = close + self.config.sl_atr_mult * atr
        tp_approx = close - self.config.tp_atr_mult * atr

        instrument = self.cache.instrument(iid)
        prec = instrument.price_precision if instrument else 2

        qty = self._qty_for(iid, close, atr)
        if qty is None:
            return

        tags: list[str] = [f"DET:{detector}"]
        if self.config.sl_atr_mult > 0:
            tags.append(f"SL:{sl_approx:.{prec}f}")
        if self.config.tp_atr_mult > 0:
            tags.append(f"TP:{tp_approx:.{prec}f}")

        order = self.order_factory.market(
            instrument_id=iid,
            order_side=OrderSide.SELL,
            quantity=qty,
            tags=tags,
        )
        self.submit_order(order)
        self._log.info(
            f"SHORT[{detector}] {iid}  bar={bar.close}  qty={qty}"
            f"  fast={self._fast_emas[iid].value:.2f}  slow={self._slow_emas[iid].value:.2f}"
            f"  atr={atr:.2f}  sl={sl_approx:.{prec}f}  tp={tp_approx:.{prec}f}"
            f"  [pos: {per_instrument+1}/{self.config.max_per_instrument}"
            f"  total: {total+1}/{self.config.max_total}]"
        )

    def _close_all(self, iid: InstrumentId, reason: str = "EMA") -> None:
        for pos in self.cache.positions_open(instrument_id=iid, strategy_id=self.id):
            if pos.id in self._pending_close:
                continue
            self._clear_pos(pos.id, reason)
            self.close_position(pos)
            self._log.info(f"CLOSE {iid}  pos={pos.id}")
