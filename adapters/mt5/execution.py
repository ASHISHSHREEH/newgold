"""
MT5ExecutionClient — LiveExecutionClient backed by MetaTrader5 package.

Threading model
---------------
All mt5.* calls live in a single daemon worker thread.  The event loop
submits work via _req_queue (asyncio.Future is resolved back on the loop).

Order flow (market orders)
--------------------------
1. _submit_order queues ("submit", command, future)
2. Worker calls mt5.order_send(); resolves future with
   (retcode, order_ticket, deal_ticket, fill_price, fill_volume, mt5_pos_ticket, err)
3. Event loop generates OrderAccepted then OrderFilled.
   venue_position_id = PositionId(str(mt5_pos_ticket))

Closing positions
-----------------
close_position(pos) from the strategy creates a SELL MARKET order.
In HEDGING mode Nautilus sets position.id = the venue_position_id passed to
generate_order_filled, so command.position_id.value is the MT5 ticket directly.
The worker passes it as position= in order_send.
"""

import asyncio
import queue
import threading
import time
from typing import Any

import MetaTrader5 as mt5

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.enums import LogColor
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import (
    CancelAllOrders,
    CancelOrder,
    GenerateFillReports,
    GenerateOrderStatusReport,
    GenerateOrderStatusReports,
    GeneratePositionStatusReports,
    ModifyOrder,
    SubmitOrder,
)
from nautilus_trader.execution.reports import (
    FillReport,
    OrderStatusReport,
    PositionStatusReport,
)
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import (
    AccountType,
    LiquiditySide,
    OmsType,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    InstrumentId,
    PositionId,
    TradeId,
    VenueOrderId,
    Venue,
)
from nautilus_trader.model.objects import AccountBalance, Currency, Money, Price, Quantity

from adapters.mt5.exec_config import MT5ExecClientConfig


class MT5ExecutionClient(LiveExecutionClient):
    """Live execution client for MetaTrader5 (FxPro demo/live)."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client_id: ClientId,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        instrument_provider: InstrumentProvider,
        config: MT5ExecClientConfig,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=client_id,
            venue=Venue("FXPRO"),
            oms_type=OmsType.HEDGING,
            account_type=AccountType.MARGIN,
            base_currency=None,  # multi-currency (JPY account, USD instruments)
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
        self._config = config
        self._stop_event = threading.Event()
        self._req_queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._ready = threading.Event()
        self._poll_interval = config.poll_interval_secs

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def _connect(self) -> None:
        self._stop_event.clear()
        self._ready.clear()
        self._worker = threading.Thread(
            target=self._run_worker,
            name="mt5-exec-worker",
            daemon=True,
        )
        self._worker.start()
        await self._loop.run_in_executor(None, self._ready.wait, 10.0)

        # account_id issuer must match the client_id value ("MT5").
        self._set_account_id(AccountId("MT5-001"))

        if not self._ready.is_set() or not self._worker.is_alive():
            self._log.error(
                "MT5 exec worker failed to start within 10 seconds — "
                "check that the MetaTrader5 terminal is running and Algo Trading is enabled"
            )
            from nautilus_trader.model.currencies import JPY
            self.generate_account_state(
                balances=[AccountBalance(total=Money(0, JPY), locked=Money(0, JPY), free=Money(0, JPY))],
                margins=[],
                reported=False,
                ts_event=self._clock.timestamp_ns(),
            )
            return
        self._log.info("MT5 execution client connected", LogColor.GREEN)
        # Report initial account state
        fut: asyncio.Future = self._loop.create_future()
        self._req_queue.put_nowait(("account", fut))
        try:
            state = await asyncio.wait_for(fut, timeout=10.0)
        except asyncio.TimeoutError:
            self._log.warning("Initial account query timed out -- using zero-balance placeholder")
            from nautilus_trader.model.currencies import JPY
            self.generate_account_state(
                balances=[AccountBalance(total=Money(0, JPY), locked=Money(0, JPY), free=Money(0, JPY))],
                margins=[],
                reported=False,
                ts_event=self._clock.timestamp_ns(),
            )
            return
        if state:
            balances, ts = state
            self.generate_account_state(
                balances=balances,
                margins=[],
                reported=True,
                ts_event=ts,
            )

    async def _disconnect(self) -> None:
        self._stop_event.set()
        if self._worker is not None:
            self._worker.join(timeout=10.0)
            self._worker = None

    # -------------------------------------------------------------------------
    # Order commands
    # -------------------------------------------------------------------------

    async def _submit_order(self, command: SubmitOrder) -> None:
        # In HEDGING mode Nautilus sets position.id = venue_position_id from the
        # opening fill, so command.position_id.value is already the MT5 ticket.
        mt5_close_ticket: int | None = None
        if command.position_id is not None:
            try:
                mt5_close_ticket = int(command.position_id.value)
            except (ValueError, TypeError):
                pass

        fut: asyncio.Future = self._loop.create_future()
        self._req_queue.put_nowait(("submit", command, mt5_close_ticket, fut))
        try:
            result = await asyncio.wait_for(fut, timeout=30.0)
        except asyncio.TimeoutError:
            self.generate_order_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=command.order.client_order_id,
                reason="Order submission timed out",
                ts_event=self._clock.timestamp_ns(),
            )
            return
        except Exception as exc:
            self.generate_order_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=command.order.client_order_id,
                reason=str(exc),
                ts_event=self._clock.timestamp_ns(),
            )
            return

        retcode, order_ticket, deal_ticket, fill_price, fill_volume, mt5_pos_ticket, err = result
        ts = self._clock.timestamp_ns()

        if retcode != mt5.TRADE_RETCODE_DONE:
            self.generate_order_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=command.order.client_order_id,
                reason=f"MT5 retcode {retcode}: {err}",
                ts_event=ts,
            )
            return

        venue_order_id = VenueOrderId(str(order_ticket))
        self.generate_order_accepted(
            strategy_id=command.strategy_id,
            instrument_id=command.instrument_id,
            client_order_id=command.order.client_order_id,
            venue_order_id=venue_order_id,
            ts_event=ts,
        )

        venue_position_id = PositionId(str(mt5_pos_ticket)) if mt5_pos_ticket else None

        symbol = command.order.instrument_id.symbol.value
        price_prec = self._config.price_precisions.get(symbol, 2)
        size_prec = self._config.size_precisions.get(symbol, 2)

        self.generate_order_filled(
            strategy_id=command.strategy_id,
            instrument_id=command.instrument_id,
            client_order_id=command.order.client_order_id,
            venue_order_id=venue_order_id,
            venue_position_id=venue_position_id,
            trade_id=TradeId(str(deal_ticket)),
            order_side=command.order.side,
            order_type=command.order.order_type,
            last_qty=Quantity(fill_volume, size_prec),
            last_px=Price(fill_price, price_prec),
            quote_currency=USD,
            commission=Money(0, USD),
            liquidity_side=LiquiditySide.TAKER,
            ts_event=ts,
        )

        # Refresh account state after each fill
        self._req_queue.put_nowait(("account_async",))

    async def _cancel_order(self, command: CancelOrder) -> None:
        # MT5 market orders fill immediately; no open orders to cancel for this strategy
        self._log.warning(
            f"cancel_order called for {command.client_order_id} — "
            "market orders on MT5 fill immediately, nothing to cancel"
        )

    async def _cancel_all_orders(self, command: CancelAllOrders) -> None:
        pass  # no pending orders to cancel

    async def _modify_order(self, command: ModifyOrder) -> None:
        raise NotImplementedError("ModifyOrder not supported for MT5 market orders")

    # -------------------------------------------------------------------------
    # Reconciliation (called at startup by the exec engine)
    # -------------------------------------------------------------------------

    async def generate_order_status_report(
        self, command: GenerateOrderStatusReport
    ) -> OrderStatusReport | None:
        return None

    async def generate_order_status_reports(
        self, command: GenerateOrderStatusReports
    ) -> list[OrderStatusReport]:
        return []

    async def generate_fill_reports(
        self, command: GenerateFillReports
    ) -> list[FillReport]:
        return []

    async def generate_position_status_reports(
        self, command: GeneratePositionStatusReports
    ) -> list[PositionStatusReport]:
        return []

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
            return

        self._log.info("MT5 exec worker started")
        self._ready.set()

        while not self._stop_event.is_set():
            try:
                self._drain_queue()
            except Exception as exc:
                self._log.error(f"MT5 exec worker error (continuing): {exc}")
            time.sleep(self._poll_interval)

        mt5.shutdown()
        self._log.info("MT5 exec worker stopped")

    def _drain_queue(self) -> None:
        while True:
            try:
                item = self._req_queue.get_nowait()
            except queue.Empty:
                break

            kind = item[0]

            if kind == "submit":
                _, command, mt5_close_ticket, fut = item
                result = self._send_order(command, mt5_close_ticket)
                self._loop.call_soon_threadsafe(_resolve_future, fut, result, None)

            elif kind == "account":
                _, fut = item
                state = self._query_account_state()
                self._loop.call_soon_threadsafe(_resolve_future, fut, state, None)

            elif kind == "account_async":
                # Fire-and-forget account refresh after a fill
                state = self._query_account_state()
                if state:
                    balances, ts = state
                    self._loop.call_soon_threadsafe(
                        self._deferred_account_state, balances, ts
                    )

    def _send_order(self, command: SubmitOrder, mt5_close_ticket: int | None) -> tuple:
        """
        Build and send an MT5 order.  Returns:
        (retcode, order_ticket, deal_ticket, fill_price, fill_volume, mt5_pos_ticket, err_msg)

        mt5_close_ticket is resolved on the event-loop thread before this is called.
        """
        order = command.order
        symbol = order.instrument_id.symbol.value

        # Determine MT5 order type
        if order.side == OrderSide.BUY:
            tick = mt5.symbol_info_tick(symbol)
            price = tick.ask if tick else 0.0
            mt5_order_type = mt5.ORDER_TYPE_BUY
        else:
            tick = mt5.symbol_info_tick(symbol)
            price = tick.bid if tick else 0.0
            mt5_order_type = mt5.ORDER_TYPE_SELL

        volume = float(order.quantity)

        sl: float | None = None
        tp: float | None = None
        if order.tags:
            for tag in order.tags:
                if tag.startswith("SL:"):
                    sl = float(tag[3:])
                elif tag.startswith("TP:"):
                    tp = float(tag[3:])

        request: dict[str, Any] = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": mt5_order_type,
            "price": price,
            "deviation": 50,  # 50 points = 0.50 USD; prevents timeout on small price moves
            "comment": str(order.client_order_id)[:31],  # MT5 comment max 31 chars
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if sl is not None:
            request["sl"] = round(sl, 5)
        if tp is not None:
            request["tp"] = round(tp, 5)

        # For closing a specific position in hedging mode (ticket resolved on event-loop)
        if mt5_close_ticket is not None:
            request["position"] = mt5_close_ticket

        result = mt5.order_send(request)
        if result is None:
            err = mt5.last_error()
            return (0, 0, 0, 0.0, 0.0, 0, f"order_send returned None: {err}")

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            # For retail hedging, new position ticket = order ticket
            mt5_pos_ticket = result.order
            return (
                result.retcode,
                result.order,
                result.deal,
                result.price,
                result.volume,
                mt5_pos_ticket,
                result.comment,
            )

        # A close can be rejected (e.g. retcode 10013) when the position was
        # already closed at the venue by a broker-side SL/TP that fired first.
        # If the position is genuinely gone, synthesize the close from its real
        # closing deal so the cache stays in sync instead of orphaning it.
        if mt5_close_ticket is not None:
            synthesized = self._synthesize_external_close(mt5_close_ticket)
            if synthesized is not None:
                self._log.warning(
                    f"Close for position {mt5_close_ticket} returned retcode "
                    f"{result.retcode} ({result.comment}); position already closed "
                    f"at venue — synthesizing close fill from its OUT deal"
                )
                return synthesized

        return (result.retcode, 0, 0, 0.0, 0.0, 0, result.comment)

    def _synthesize_external_close(self, close_ticket: int) -> tuple | None:
        """
        Resolve a rejected close when the position has already been closed at the
        venue (typically a broker-side SL/TP that fired before our order).

        Returns a result tuple shaped like a successful _send_order so the caller
        emits a normal fill and flattens the cached position, or None if the
        position still exists (a genuine rejection) or cannot be confirmed closed.
        """
        still_open = mt5.positions_get(ticket=close_ticket)
        if still_open is None:
            return None  # query error — do not assume the position is gone
        if len(still_open) > 0:
            return None  # genuinely still open — a real rejection

        # Position is gone — find its closing (DEAL_ENTRY_OUT) deal. Usually
        # already in history; retry briefly in case we raced the broker.
        out_deal = None
        for _ in range(5):
            deals = mt5.history_deals_get(position=close_ticket)
            if deals:
                outs = [d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
                if outs:
                    out_deal = max(outs, key=lambda d: d.time_msc)
                    break
            time.sleep(0.2)

        if out_deal is None:
            return None  # cannot confirm the close — fall back to rejection

        return (
            mt5.TRADE_RETCODE_DONE,
            out_deal.order or close_ticket,  # venue_order_id
            out_deal.ticket,                 # trade_id (unique deal id)
            out_deal.price,
            out_deal.volume,
            close_ticket,                    # venue_position_id == cached position id
            f"position {close_ticket} already closed at venue; synthesized close",
        )

    def _query_account_state(self) -> tuple | None:
        info = mt5.account_info()
        if info is None:
            return None
        from nautilus_trader.model.currencies import JPY
        ts = self._clock.timestamp_ns()
        jpy_bal = Money(info.balance, JPY)
        jpy_locked = Money(info.margin, JPY)
        # Use balance - margin for free so total - locked == free always holds.
        # (MT5's margin_free includes unrealized P&L which breaks Nautilus validation.)
        jpy_free = Money(info.balance - info.margin, JPY)
        balances = [AccountBalance(total=jpy_bal, locked=jpy_locked, free=jpy_free)]
        return (balances, ts)

    def _deferred_account_state(self, balances, ts) -> None:
        """Called on the event loop thread to publish updated account state."""
        self.generate_account_state(balances=balances, margins=[], reported=True, ts_event=ts)


def _resolve_future(fut: asyncio.Future, result: Any, exc: Exception | None) -> None:
    if fut.done():
        return
    if exc is not None:
        fut.set_exception(exc)
    else:
        fut.set_result(result)
