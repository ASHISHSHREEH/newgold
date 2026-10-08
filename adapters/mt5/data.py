"""
MT5DataClient — LiveMarketDataClient backed by the MetaTrader5 package.

Threading model
---------------
MetaTrader5 is synchronous and not thread-safe.  Every mt5.* call lives in a
single daemon thread (_run_worker).  The asyncio event loop never touches mt5
directly.

Tick delivery: worker calls loop.call_soon_threadsafe(_handle_data_py, quote).
Bar requests:  worker resolves an asyncio.Future via call_soon_threadsafe; the
               awaiting coroutine then calls _handle_bars_py on the loop thread.
"""

import asyncio
import queue
import threading
import time
from typing import Any

import MetaTrader5 as mt5

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.data.messages import (
    RequestBars,
    SubscribeBars,
    SubscribeQuoteTicks,
    UnsubscribeBars,
    UnsubscribeQuoteTicks,
)
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.data import Bar, BarType, QuoteTick
from nautilus_trader.model.enums import BarAggregation
from nautilus_trader.model.identifiers import ClientId, InstrumentId, Venue
from nautilus_trader.model.objects import Price, Quantity

from adapters.mt5.config import MT5DataClientConfig

# Map (BarAggregation, step) → MT5 timeframe constant.
# Constants are plain integers in the MT5 package; accessible without initialize().
_TF = {
    (BarAggregation.MINUTE, 1):  mt5.TIMEFRAME_M1,
    (BarAggregation.MINUTE, 2):  mt5.TIMEFRAME_M2,
    (BarAggregation.MINUTE, 3):  mt5.TIMEFRAME_M3,
    (BarAggregation.MINUTE, 4):  mt5.TIMEFRAME_M4,
    (BarAggregation.MINUTE, 5):  mt5.TIMEFRAME_M5,
    (BarAggregation.MINUTE, 6):  mt5.TIMEFRAME_M6,
    (BarAggregation.MINUTE, 10): mt5.TIMEFRAME_M10,
    (BarAggregation.MINUTE, 12): mt5.TIMEFRAME_M12,
    (BarAggregation.MINUTE, 15): mt5.TIMEFRAME_M15,
    (BarAggregation.MINUTE, 20): mt5.TIMEFRAME_M20,
    (BarAggregation.MINUTE, 30): mt5.TIMEFRAME_M30,
    (BarAggregation.HOUR, 1):    mt5.TIMEFRAME_H1,
    (BarAggregation.HOUR, 2):    mt5.TIMEFRAME_H2,
    (BarAggregation.HOUR, 3):    mt5.TIMEFRAME_H3,
    (BarAggregation.HOUR, 4):    mt5.TIMEFRAME_H4,
    (BarAggregation.HOUR, 6):    mt5.TIMEFRAME_H6,
    (BarAggregation.HOUR, 8):    mt5.TIMEFRAME_H8,
    (BarAggregation.HOUR, 12):   mt5.TIMEFRAME_H12,
    (BarAggregation.DAY, 1):     mt5.TIMEFRAME_D1,
    (BarAggregation.WEEK, 1):    mt5.TIMEFRAME_W1,
    (BarAggregation.MONTH, 1):   mt5.TIMEFRAME_MN1,
}


class MT5DataClient(LiveMarketDataClient):
    """
    Live data client for MetaTrader5 (FxPro demo).

    Subscribes to quote ticks via polling and fulfils historical bar requests
    via copy_rates_from_pos.  All MT5 calls are isolated to a single worker
    thread; no mt5.* call appears inside any async method.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client_id: ClientId,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        instrument_provider: InstrumentProvider,
        config: MT5DataClientConfig,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=client_id,
            venue=Venue("FXPRO"),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
            config=config,
        )
        self._config = config
        self._stop_event = threading.Event()
        self._sub_lock = threading.Lock()
        self._subscribed: set[InstrumentId] = set()
        # _last_msc tracks the last seen time_msc per symbol; only the worker
        # thread writes/reads this, so no lock is needed.
        self._last_msc: dict[str, int] = {}
        self._req_queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._ready = threading.Event()  # set by worker after mt5.initialize() succeeds
        # bar subscriptions: bar_type → last published bar time (unix seconds, 0 = not yet published)
        self._sub_bars_lock = threading.Lock()
        self._subscribed_bars: dict[BarType, int] = {}

    # -------------------------------------------------------------------------
    # Lifecycle (called on the event loop thread by LiveMarketDataClient)
    # -------------------------------------------------------------------------

    async def _connect(self) -> None:
        self._stop_event.clear()
        self._ready.clear()
        self._worker = threading.Thread(
            target=self._run_worker,
            name="mt5-data-worker",
            daemon=True,
        )
        self._worker.start()
        # Block the event loop minimally; the executor runs _ready.wait in a
        # thread-pool thread so the loop stays responsive.
        await self._loop.run_in_executor(None, self._ready.wait, 10.0)
        if not self._ready.is_set() or not self._worker.is_alive():
            self._log.error(
                "MT5 worker failed to initialise within 10 seconds — "
                "check that the MetaTrader5 terminal is running"
            )
            return
        self._log.info(
            f"MT5 worker thread started (poll_interval={self._config.poll_interval_secs}s)"
        )

    async def _disconnect(self) -> None:
        self._stop_event.set()
        if self._worker is not None:
            self._worker.join(timeout=10.0)
            self._worker = None

    # -------------------------------------------------------------------------
    # Subscriptions (event loop thread; no MT5 calls allowed here)
    # -------------------------------------------------------------------------

    async def _subscribe_quote_ticks(self, command: SubscribeQuoteTicks) -> None:
        with self._sub_lock:
            self._subscribed.add(command.instrument_id)

    async def _unsubscribe_quote_ticks(self, command: UnsubscribeQuoteTicks) -> None:
        with self._sub_lock:
            self._subscribed.discard(command.instrument_id)
            self._last_msc.pop(command.instrument_id.symbol.value, None)

    async def _subscribe_bars(self, command: SubscribeBars) -> None:
        bar_type = command.bar_type
        with self._sub_bars_lock:
            if bar_type not in self._subscribed_bars:
                self._subscribed_bars[bar_type] = 0
        self._add_subscription_bars(bar_type)

    async def _unsubscribe_bars(self, command: UnsubscribeBars) -> None:
        bar_type = command.bar_type
        with self._sub_bars_lock:
            self._subscribed_bars.pop(bar_type, None)
        self._remove_subscription_bars(bar_type)

    # -------------------------------------------------------------------------
    # Historical bar request (event loop thread; MT5 work delegated to worker)
    # -------------------------------------------------------------------------

    async def _request_bars(self, request: RequestBars) -> None:
        fut: asyncio.Future = self._loop.create_future()
        self._req_queue.put_nowait(("bars", request, fut))
        try:
            bars: list[Bar] = await asyncio.wait_for(fut, timeout=30.0)
        except asyncio.TimeoutError:
            self._log.error(f"Bar request timed out after 30 seconds for {request.bar_type}")
            return
        except Exception as exc:
            self._log.error(f"Bar request failed for {request.bar_type}: {exc}")
            return
            
        self._handle_bars_py(
            request.bar_type,
            bars,
            request.id,
            request.start,
            request.end,
            request.params,
        )

    # -------------------------------------------------------------------------
    # Worker thread — the ONLY place mt5.* may be called
    # -------------------------------------------------------------------------

    def _run_worker(self) -> None:
        init_kwargs: dict = {}
        if self._config.terminal_path:
            init_kwargs["path"] = self._config.terminal_path
        if self._config.login is not None:
            init_kwargs["login"] = self._config.login
        if self._config.password:
            init_kwargs["password"] = self._config.password
        if self._config.server:
            init_kwargs["server"] = self._config.server
        if not mt5.initialize(**init_kwargs):
            self._log.error(f"mt5.initialize() failed: {mt5.last_error()}")
            return  # _ready stays unset; _connect will detect thread death

        self._log.info(
            f"MT5 connected; poll_interval={self._config.poll_interval_secs}s"
        )
        self._ready.set()
        interval = self._config.poll_interval_secs

        while not self._stop_event.is_set():
            try:
                self._drain_request_queue()
                self._poll_ticks()
                self._poll_bars()
            except Exception as exc:  # noqa: BLE001
                self._log.error(f"MT5 worker loop error (continuing): {exc}")
            time.sleep(interval)

        mt5.shutdown()
        self._log.info("MT5 worker thread stopped")

    def _drain_request_queue(self) -> None:
        while True:
            try:
                kind, req, fut = self._req_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "bars":
                    result = self._fetch_bars(req)
                    self._loop.call_soon_threadsafe(
                        self._resolve_future, fut, result, None
                    )
                else:
                    self._loop.call_soon_threadsafe(
                        self._resolve_future, fut, None, ValueError(f"unknown kind: {kind}")
                    )
            except Exception as exc:
                self._loop.call_soon_threadsafe(
                    self._resolve_future, fut, None, exc
                )

    def _fetch_bars(self, request: RequestBars) -> list[Bar]:
        """Run copy_rates_from_pos and convert to Bar objects."""
        spec = request.bar_type.spec
        tf = _TF.get((spec.aggregation, spec.step))
        if tf is None:
            raise ValueError(
                f"No MT5 timeframe for {spec.aggregation} step={spec.step}"
            )

        symbol = request.bar_type.instrument_id.symbol.value
        count = request.limit if request.limit > 0 else 1000

        # pos=1 skips the currently forming bar (pos=0); warmup should only
        # include completed bars, consistent with what _poll_bars delivers.
        rates = mt5.copy_rates_from_pos(symbol, tf, 1, count)
        if rates is None:
            raise RuntimeError(
                f"copy_rates_from_pos({symbol}) failed: {mt5.last_error()}"
            )

        prec = self._config.price_precisions.get(symbol, 2)
        ts_init = self._clock.timestamp_ns()
        bars: list[Bar] = []
        for row in rates:
            ts_event = int(row["time"]) * 1_000_000_000  # UTC seconds → ns
            bars.append(
                Bar(
                    bar_type=request.bar_type,
                    open=Price(float(row["open"]), prec),
                    high=Price(float(row["high"]), prec),
                    low=Price(float(row["low"]), prec),
                    close=Price(float(row["close"]), prec),
                    volume=Quantity(float(row["tick_volume"]), 0),
                    ts_event=ts_event,
                    ts_init=ts_init,
                )
            )
        return bars

    def _poll_bars(self) -> None:
        """Publish a new completed bar for each subscribed bar type when available."""
        with self._sub_bars_lock:
            snapshot = dict(self._subscribed_bars)

        ts_init = self._clock.timestamp_ns()

        for bar_type, last_time in snapshot.items():
            spec = bar_type.spec
            tf = _TF.get((spec.aggregation, spec.step))
            if tf is None:
                continue
            symbol = bar_type.instrument_id.symbol.value
            prec = self._config.price_precisions.get(symbol, 2)
            # pos=1 is the last *completed* bar (pos=0 is still forming).
            rates = mt5.copy_rates_from_pos(symbol, tf, 1, 1)
            if rates is None or len(rates) == 0:
                continue
            bar_time = int(rates[0]["time"])
            if bar_time <= last_time:
                continue  # no new bar since last publish
            bar = Bar(
                bar_type=bar_type,
                open=Price(float(rates[0]["open"]), prec),
                high=Price(float(rates[0]["high"]), prec),
                low=Price(float(rates[0]["low"]), prec),
                close=Price(float(rates[0]["close"]), prec),
                volume=Quantity(float(rates[0]["tick_volume"]), 0),
                ts_event=bar_time * 1_000_000_000,
                ts_init=ts_init,
            )
            with self._sub_bars_lock:
                self._subscribed_bars[bar_type] = bar_time
            self._log.debug(f"New bar {bar_type} close={bar.close}")
            self._loop.call_soon_threadsafe(self._handle_data_py, bar)

    def _poll_ticks(self) -> None:
        """Poll every subscribed symbol and publish any new ticks."""
        with self._sub_lock:
            instruments = list(self._subscribed)

        if not instruments:
            return

        now_ns = self._clock.timestamp_ns()

        for instrument_id in instruments:
            symbol = instrument_id.symbol.value
            prec = self._config.price_precisions.get(symbol, 2)
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                continue

            msc: int = tick.time_msc
            if self._last_msc.get(symbol) == msc:
                continue  # no new tick since last poll
            self._last_msc[symbol] = msc

            ts_event = msc * 1_000_000  # ms → ns
            age_ms = (now_ns - ts_event) / 1_000_000
            self._log.debug(
                f"Tick {symbol} bid={tick.bid} ask={tick.ask} age={age_ms:.1f}ms"
            )

            quote = QuoteTick(
                instrument_id=instrument_id,
                bid_price=Price(tick.bid, prec),
                ask_price=Price(tick.ask, prec),
                bid_size=Quantity(1, 0),
                ask_size=Quantity(1, 0),
                ts_event=ts_event,
                ts_init=now_ns,
            )
            self._loop.call_soon_threadsafe(self._handle_data_py, quote)

    # -------------------------------------------------------------------------
    # Helpers (called on the event loop thread via call_soon_threadsafe)
    # -------------------------------------------------------------------------

    @staticmethod
    def _resolve_future(
        fut: asyncio.Future,
        result: Any,
        exc: Exception | None,
    ) -> None:
        if fut.done():
            return
        if exc is not None:
            fut.set_exception(exc)
        else:
            fut.set_result(result)