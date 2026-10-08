"""
MonitorActor — writes trade history to SQLite and current state to live_state.json.

Attaches to the NautilusTrader event loop via node.trader.add_actor(MonitorActor(...)).
Subscribes directly on the message bus for position events (topic "events.position.*")
so it works independently of the strategy's on_position_* callbacks.

Data dir (relative to this file): data/
  data/trading_mt5.db   — SQLite, tables: sessions, trades
  data/live_state.json  — refreshed on every bar
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import PositionClosed, PositionOpened
from nautilus_trader.model.identifiers import InstrumentId, Venue

import monitor_shared
from adapters.mt5.instrument import PRICE_PRECISIONS

_DATA_DIR = Path(__file__).parent / "data"
_DB_NAME = "trading_mt5.db"
_STATE_NAME = "live_state.json"
_BARS_DIR = _DATA_DIR / "bars"


class MonitorActorConfig(ActorConfig, frozen=True):
    instruments: tuple[str, ...] = ()
    bar_spec: str = "15-MINUTE-LAST-EXTERNAL"
    venue: str = "MT5"
    # Archive every received bar to Parquet (data/bars/) for future ML feature
    # engineering. Bars are buffered and flushed every `bar_flush_every` bars
    # (and on stop) — row-by-row Parquet appends are inefficient.
    bar_archive: bool = True
    bar_flush_every: int = 24


class MonitorActor(Actor):
    def __init__(self, config: MonitorActorConfig) -> None:
        super().__init__(config)
        self._iids = [InstrumentId.from_str(s) for s in config.instruments]
        self._venue = Venue(config.venue)
        self._bar_spec = config.bar_spec
        self._session_id: int | None = None
        self._lock = threading.Lock()
        self._last_prices: dict[str, float] = {}
        self._bar_buffer: list[dict] = []
        self._bar_file_counter = 0

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def on_start(self) -> None:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._session_id = self._new_session()

        # Subscribe to all position events published by any strategy
        self._msgbus.subscribe(
            topic="events.position.*",
            handler=self._handle_position_event,
        )

        # Subscribe to bars so we can refresh live_state.json periodically
        for iid in self._iids:
            bar_type = BarType.from_str(f"{iid}-{self._bar_spec}")
            self.subscribe_bars(bar_type)

        # Write immediately so the dashboard shows real data from the first request,
        # not zeros (live_state.json would otherwise stay absent until the first bar
        # which arrives up to 15 minutes after startup).
        self._write_live_state()

        self._log.info(f"MonitorActor started  session_id={self._session_id}")

    def on_stop(self) -> None:
        self._msgbus.unsubscribe(
            topic="events.position.*",
            handler=self._handle_position_event,
        )
        if self.config.bar_archive:
            self._flush_bars()
        self._close_session()
        self._log.info("MonitorActor stopped")

    # ── Position event dispatcher (msgbus handler) ─────────────────────────────

    def _handle_position_event(self, event) -> None:
        if isinstance(event, PositionOpened):
            self._on_position_opened(event)
        elif isinstance(event, PositionClosed):
            self._on_position_closed(event)

    # ── Position open ──────────────────────────────────────────────────────────

    def _on_position_opened(self, event: PositionOpened) -> None:
        pid = str(event.position_id)
        symbol = event.instrument_id.symbol.value
        direction = "BUY" if event.entry == OrderSide.BUY else "SELL"
        open_time = _ts_to_iso(event.ts_event)
        entry = event.avg_px_open
        lot_size = float(event.quantity)
        sltp = monitor_shared.get_pos_sltp(pid)
        feats = monitor_shared.pop_entry_features(pid)

        self._db_exec(
            "INSERT OR IGNORE INTO trades "
            "(ticket, symbol, direction, open_time, entry_price, lot_size, sl, tp, "
            " fast_ema, slow_ema, atr_entry) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pid, symbol, direction, open_time, entry,
             lot_size, sltp.get("sl"), sltp.get("tp"),
             feats.get("fast_ema"), feats.get("slow_ema"), feats.get("atr")),
        )
        self._log.info(f"MONITOR opened  {pid}  {symbol}  {direction}  entry={entry}")
        self._write_live_state()

    # ── Position close ─────────────────────────────────────────────────────────

    def _on_position_closed(self, event: PositionClosed) -> None:
        pid = str(event.position_id)
        close_time = _ts_to_iso(event.ts_event)
        exit_price = event.avg_px_close
        # Read the live (trailed) SL/TP levels BEFORE removing them — needed to
        # reconcile the close reason against where the position actually closed.
        sltp = monitor_shared.get_pos_sltp(pid)
        reason = monitor_shared.pop_close_reason(pid)
        reason = self._reconcile_reason(reason, exit_price, sltp)
        monitor_shared.remove_pos_sltp(pid)
        exc = monitor_shared.pop_excursion(pid)
        duration_ns = getattr(event, "duration_ns", None)
        duration_sec = (duration_ns / 1_000_000_000) if duration_ns else None

        # realized_pnl is in quote currency (USD for most instruments)
        profit: float | None = None
        rr: float | None = None
        pos = self.cache.position(event.position_id)
        if pos is not None and pos.realized_pnl is not None:
            profit = pos.realized_pnl.as_double()
            row = self._db_query(
                "SELECT sl, entry_price FROM trades WHERE ticket=?", (pid,)
            )
            if row and row[0]["sl"] is not None and event.avg_px_open > 0:
                risk = abs(event.avg_px_open - row[0]["sl"])
                if risk > 0 and profit is not None:
                    rr = round(profit / risk, 2)

        self._db_exec(
            "UPDATE trades SET close_time=?, exit_price=?, profit=?, exit_reason=?, rr_ratio=?, "
            "mae=?, mfe=?, duration_sec=? WHERE ticket=?",
            (close_time, exit_price, profit, reason, rr,
             exc.get("mae"), exc.get("mfe"), duration_sec, pid),
        )
        self._log.info(
            f"MONITOR closed  {pid}  reason={reason}  profit={profit}  rr={rr}"
        )
        self._write_live_state()

    # ── Close-reason reconciliation ────────────────────────────────────────────

    @staticmethod
    def _reconcile_reason(reason: str, exit_price, sltp: dict) -> str:
        """Correct a price-level close reason using the actual exit price.

        The strategy records "TP"/"SL" from its *internal* levels at the moment
        it submits the close — but the position may already have been closed at
        the venue by a broker-side SL/TP sitting at slightly different prices
        (the strategy's trailing stop is not mirrored to the broker). That makes
        the strategy's guess unreliable: e.g. it logs "SL" after its trail check
        fires, while the venue actually closed the position at its TP.

        The exit price is ground truth. For a price-level exit, attribute it to
        whichever of SL/TP the fill landed closest to. Event-driven reasons
        (EMA crossover, SHUTDOWN, CLOSED) are not price levels and are trusted.
        """
        if reason not in ("TP", "SL"):
            return reason
        sl = sltp.get("sl")
        tp = sltp.get("tp")
        if exit_price is None or sl is None or tp is None:
            return reason
        return "TP" if abs(exit_price - tp) <= abs(exit_price - sl) else "SL"

    # ── Bar handler — refresh live_state.json ──────────────────────────────────

    def on_bar(self, bar: Bar) -> None:
        sym = bar.bar_type.instrument_id.symbol.value
        self._last_prices[sym] = float(bar.close)
        if self.config.bar_archive:
            self._buffer_bar(bar)
        self._write_live_state()

    # ── Bar archive → Parquet (data/bars/) ─────────────────────────────────────

    def _buffer_bar(self, bar: Bar) -> None:
        self._bar_buffer.append({
            "symbol":      bar.bar_type.instrument_id.symbol.value,
            "bar_type":    str(bar.bar_type),
            "ts_event":    _ts_to_iso(bar.ts_event),
            "ts_event_ns": int(bar.ts_event),
            "open":        float(bar.open),
            "high":        float(bar.high),
            "low":         float(bar.low),
            "close":       float(bar.close),
            "volume":      float(bar.volume),
        })
        if len(self._bar_buffer) >= self.config.bar_flush_every:
            self._flush_bars()

    def _flush_bars(self) -> None:
        """Write the buffered bars to a new Parquet part file and clear the buffer.

        Each flush is a separate file under data/bars/ (columnar formats don't
        append row-by-row); read them all back with pandas.read_parquet("data/bars").
        """
        if not self._bar_buffer:
            return
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq

            table = pa.Table.from_pylist(self._bar_buffer)
            _BARS_DIR.mkdir(parents=True, exist_ok=True)
            path = _BARS_DIR / f"bars_s{self._session_id or 0}_{self._bar_file_counter:05d}.parquet"
            pq.write_table(table, str(path))
            self._bar_file_counter += 1
            self._log.info(f"Archived {len(self._bar_buffer)} bars → {path.name}")
            self._bar_buffer.clear()
        except Exception as exc:
            self._log.error(f"Bar archive flush failed: {exc}")

    # ── Live state JSON ────────────────────────────────────────────────────────

    def _read_balance(self) -> float:
        try:
            acct = self.cache.account_for_venue(self._venue)
            if acct is None:
                self._log.warning("account_for_venue returned None — balance will be 0")
                return 0.0
            # balances_total() → dict[Currency, Money]; avoids hardcoding a currency import
            totals = acct.balances_total()
            if not totals:
                self._log.warning("No balances on account — balance will be 0")
                return 0.0
            total = next(iter(totals.values()))
            try:
                return float(total.as_double())
            except Exception:
                # Money.__str__ → "90_808 JPY" — strip currency symbol and underscores
                return float(str(total).split()[0].replace("_", ""))
        except Exception as exc:
            self._log.error(f"Balance read failed: {exc}")
            return 0.0

    def _write_live_state(self) -> None:
        balance = self._read_balance()

        open_pos = []
        for iid in self._iids:
            for pos in self.cache.positions_open(instrument_id=iid):
                sym = pos.instrument_id.symbol.value
                cur_price = self._last_prices.get(sym, pos.avg_px_open)
                upnl = 0.0
                try:
                    entry_px = float(pos.avg_px_open)
                    qty      = float(pos.quantity)
                    side_mult = 1.0 if pos.entry == OrderSide.BUY else -1.0
                    upnl = side_mult * (cur_price - entry_px) * qty
                except Exception as exc:
                    self._log.warning(f"upnl calc failed for {pos.id}: {exc}")
                pid = str(pos.id)
                sltp = monitor_shared.get_pos_sltp(pid)
                open_pos.append({
                    "ticket":        pid,
                    "symbol":        sym,
                    "direction":     "BUY" if pos.entry == OrderSide.BUY else "SELL",
                    "entry_price":   round(float(pos.avg_px_open), 5),
                    "current_price": round(float(cur_price), 5),
                    "volume":        float(pos.quantity),
                    "profit":        round(float(upnl), 2),
                    "sl":            round(sltp["sl"], 2) if sltp.get("sl") is not None else None,
                    "tp":            round(sltp["tp"], 2) if sltp.get("tp") is not None else None,
                })

        state = {
            "balance":        round(balance, 2),
            "equity":         round(balance, 2),
            "open_positions": open_pos,
            "last_scan_time": datetime.now(timezone.utc).isoformat(),
            "mt5_connected":  True,
        }
        tmp = _DATA_DIR / (_STATE_NAME + ".tmp")
        try:
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            tmp.replace(_DATA_DIR / _STATE_NAME)
        except Exception as exc:
            self._log.error(f"live_state write failed: {exc}")

    # ── SQLite helpers ─────────────────────────────────────────────────────────

    def _db_path(self) -> str:
        return str(_DATA_DIR / _DB_NAME)

    def _open_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path(), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        conn = self._open_db()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_time TEXT NOT NULL,
                    end_time   TEXT
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket       TEXT UNIQUE,
                    symbol       TEXT,
                    direction    TEXT,
                    open_time    TEXT,
                    close_time   TEXT,
                    entry_price  REAL,
                    exit_price   REAL,
                    lot_size     REAL,
                    profit       REAL,
                    exit_reason  TEXT,
                    rr_ratio     REAL,
                    sl           REAL,
                    tp           REAL,
                    fast_ema     REAL,
                    slow_ema     REAL,
                    atr_entry    REAL,
                    mae          REAL,
                    mfe          REAL,
                    duration_sec REAL
                );
            """)
            conn.commit()
        finally:
            conn.close()
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Add any ML columns missing from a trades table created by an older
        build (CREATE TABLE IF NOT EXISTS won't add columns to an existing table)."""
        needed = {
            "fast_ema": "REAL", "slow_ema": "REAL", "atr_entry": "REAL",
            "mae": "REAL", "mfe": "REAL", "duration_sec": "REAL",
        }
        conn = self._open_db()
        try:
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(trades)")}
            for col, typ in needed.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
            conn.commit()
        except Exception as exc:
            self._log.error(f"Schema migration failed: {exc}")
        finally:
            conn.close()

    def _new_session(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        conn = self._open_db()
        try:
            cur = conn.execute(
                "INSERT INTO sessions (start_time) VALUES (?)", (now,)
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def _close_session(self) -> None:
        if self._session_id is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        self._db_exec(
            "UPDATE sessions SET end_time=? WHERE id=?",
            (now, self._session_id),
        )

    def _db_exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            try:
                conn = self._open_db()
                try:
                    conn.execute(sql, params)
                    conn.commit()
                finally:
                    conn.close()
            except Exception as exc:
                self._log.error(f"DB write failed: {exc}")

    def _db_query(self, sql: str, params: tuple = ()) -> list[dict]:
        try:
            conn = self._open_db()
            try:
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
            finally:
                conn.close()
        except Exception:
            return []


def _ts_to_iso(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc).isoformat()
