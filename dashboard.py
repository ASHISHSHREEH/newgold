"""
Flask monitoring dashboard for the NautilusTrader / MT5 bot.

Reads:
  data/trading_mt5.db   — SQLite with sessions + trades tables
  data/live_state.json  — live balance, open positions (written by MonitorActor)

Run:
  .venv\\Scripts\\python dashboard.py          (opens on http://127.0.0.1:5000)
  PORT=8080 .venv\\Scripts\\python dashboard.py
"""
from __future__ import annotations

import json
import os
import sqlite3
from calendar import monthcalendar
from datetime import date, datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template_string, redirect, request, url_for

_BASE      = Path(__file__).parent
DB_PATH    = _BASE / "data" / "trading_mt5.db"
STATE_PATH = _BASE / "data" / "live_state.json"
FLAG_PATH  = _BASE / "data" / "restart_request.flag"

RESTART_PASSWORD = "Anjila2004"

app = Flask(__name__)

# ── DB helpers ────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def _q(sql: str, params: tuple = ()) -> list[dict]:
    if not DB_PATH.exists():
        return []
    try:
        with _db() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []

def _qone(sql: str, params: tuple = ()) -> dict:
    rows = _q(sql, params)
    return rows[0] if rows else {}

# ── Live state ────────────────────────────────────────────────────────────────

def _live() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"balance": 0, "equity": 0, "open_positions": [],
                "last_scan_time": None, "mt5_connected": False}

def _enrich_positions(positions: list[dict]) -> list[dict]:
    """Compute unrealized P&L from price difference — works even when bot reports 0."""
    for p in positions:
        try:
            entry = float(p.get("entry_price") or 0)
            cur   = float(p.get("current_price") or entry)
            vol   = float(p.get("volume") or 0)
            sign  = 1.0 if p.get("direction") == "BUY" else -1.0
            p["profit"] = round(sign * (cur - entry) * vol, 2)
        except Exception:
            p["profit"] = 0.0
    return positions

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/debug")
def debug():
    import traceback
    out = {"db_exists": DB_PATH.exists(), "state_exists": STATE_PATH.exists(),
           "state": _live()}
    try:
        out["sessions"] = _q("SELECT * FROM sessions ORDER BY id DESC LIMIT 5")
        out["trades"]   = _q("SELECT * FROM trades ORDER BY id DESC LIMIT 5")
    except Exception:
        out["db_error"] = traceback.format_exc()
    return jsonify(out)

@app.route("/")
def index():
    try:
        return _index()
    except Exception:
        import traceback
        return (f"<pre style='background:#111;color:#f85149;padding:24px'>"
                f"Dashboard error:\n{traceback.format_exc()}\n\n"
                f"<a href='/debug' style='color:#58a6ff'>→ /debug</a></pre>"), 500

@app.route("/api/state")
def api_state():
    live = _live()
    live["open_positions"] = _enrich_positions(live.get("open_positions", []))
    return jsonify(live)

@app.route("/api/trades")
def api_trades():
    return jsonify(_q("SELECT * FROM trades ORDER BY open_time DESC LIMIT 100"))

@app.route("/restart", methods=["POST"])
def restart():
    if request.form.get("pwd") != RESTART_PASSWORD:
        return ("<pre style='background:#111;color:#f85149;padding:24px'>"
                "Wrong password.</pre>"), 403
    FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
    FLAG_PATH.touch()
    return redirect(url_for("index"))

# ── Main page builder ─────────────────────────────────────────────────────────

def _index():
    live      = _live()
    today_str = date.today().isoformat()

    balance   = live.get("balance", 0)
    mt5_ok    = live.get("mt5_connected", False)
    last_scan = live.get("last_scan_time") or "—"
    open_pos  = _enrich_positions(live.get("open_positions", []))

    bot_running = bool(_qone(
        "SELECT id FROM sessions WHERE end_time IS NULL ORDER BY id DESC LIMIT 1"))

    # ── Stats from closed trades ──────────────────────────────────────────────
    all_st = _qone(
        "SELECT COUNT(*) AS cnt, "
        "SUM(CASE WHEN profit>0 THEN 1 ELSE 0 END) AS wins, "
        "SUM(profit) AS total_profit "
        "FROM trades WHERE close_time IS NOT NULL AND profit IS NOT NULL")
    total_trades  = all_st.get("cnt") or 0
    total_wins    = all_st.get("wins") or 0
    total_profit  = all_st.get("total_profit") or 0.0
    win_rate      = round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0.0

    today_st = _qone(
        "SELECT COUNT(*) AS today_count, SUM(profit) AS today_profit, "
        "MAX(profit) AS best_today, MIN(profit) AS worst_today "
        "FROM trades WHERE date(close_time) = ? AND profit IS NOT NULL", (today_str,))
    today_count  = today_st.get("today_count")  or 0
    today_profit = today_st.get("today_profit") or 0.0
    best_today   = today_st.get("best_today")   or 0.0
    worst_today  = today_st.get("worst_today")  or 0.0

    # Unrealized P&L total
    total_unrealized = round(sum(p["profit"] for p in open_pos), 2)
    equity = round(balance + total_unrealized, 2)

    # ── Monthly P&L calendar ──────────────────────────────────────────────────
    now = datetime.now()
    cal_year, cal_month = now.year, now.month
    pnl_by_day = {r["trade_date"]: round(r["day_pnl"], 2) for r in _q(
        "SELECT date(close_time) AS trade_date, COALESCE(SUM(profit),0) AS day_pnl "
        "FROM trades WHERE close_time IS NOT NULL AND profit IS NOT NULL "
        "GROUP BY date(close_time)")}
    cal_rows = []
    for week in monthcalendar(cal_year, cal_month):
        row = []
        for d in week:
            if d == 0:
                row.append({"day": "", "pnl": None, "cls": "empty"})
            else:
                ds  = f"{cal_year:04d}-{cal_month:02d}-{d:02d}"
                pnl = pnl_by_day.get(ds)
                row.append({"day": d, "pnl": pnl,
                             "cls": ("profit" if pnl and pnl >= 0 else
                                     "loss"   if pnl and pnl < 0  else "")})
        cal_rows.append(row)

    # ── Win rate by symbol ────────────────────────────────────────────────────
    sym_data = []
    for r in _q("SELECT symbol, COUNT(*) AS total, "
                "SUM(CASE WHEN profit>0 THEN 1 ELSE 0 END) AS wins "
                "FROM trades WHERE close_time IS NOT NULL AND profit IS NOT NULL "
                "GROUP BY symbol"):
        t = r["total"] or 0
        w = r["wins"]  or 0
        sym_data.append({"symbol": r["symbol"], "total": t, "wins": w,
                          "rate": round(w / t * 100, 1) if t > 0 else 0.0})

    # ── Equity curve data (daily close) ───────────────────────────────────────
    equity_rows = _q(
        "SELECT date(close_time) AS d, SUM(profit) AS pnl "
        "FROM trades WHERE close_time IS NOT NULL AND profit IS NOT NULL "
        "GROUP BY date(close_time) ORDER BY d")
    eq_labels, eq_values, running = [], [], 0.0
    for r in equity_rows:
        running += r["pnl"] or 0
        eq_labels.append(r["d"])
        eq_values.append(round(running, 2))

    # ── Recent trades ─────────────────────────────────────────────────────────
    recent = _q(
        "SELECT ticket, symbol, direction, open_time, close_time, "
        "entry_price, exit_price, lot_size, profit, exit_reason, rr_ratio "
        "FROM trades WHERE close_time IS NOT NULL "
        "ORDER BY close_time DESC LIMIT 20")

    # Open-position count per symbol (for chart button badges)
    open_by_sym: dict[str, int] = {}
    for p in open_pos:
        s = p.get("symbol", "")
        open_by_sym[s] = open_by_sym.get(s, 0) + 1

    # TradingView mapping
    _tv = {"GOLD": "FXOPEN:XAUUSD", "#USNDAQ100": "NASDAQ:QQQ",
           "#USSPX500": "AMEX:SPY", "WTI": "TVC:USOIL",
           "BITCOIN": "BINANCE:BTCUSDT", "#Japan225": "TVC:NI225"}

    # Build chart symbol list: open-position symbols first, then historically traded
    hist_syms = [r["symbol"] for r in _q("SELECT DISTINCT symbol FROM trades ORDER BY symbol")]
    all_syms  = list(dict.fromkeys(list(open_by_sym.keys()) + hist_syms)) or ["GOLD"]

    # Default chart = first symbol with an open position (or first historical)
    default_sym = next(iter(open_by_sym), all_syms[0])
    tv_sym = _tv.get(default_sym, default_sym)

    return render_template_string(_HTML,
        balance=balance, equity=equity, mt5_ok=mt5_ok, last_scan=last_scan,
        bot_running=bot_running, open_pos=open_pos,
        win_rate=win_rate, total_trades=total_trades, total_wins=total_wins,
        total_profit=round(total_profit, 2), total_unrealized=total_unrealized,
        today_count=today_count, today_profit=round(today_profit, 2),
        best_today=round(best_today, 2), worst_today=round(worst_today, 2),
        cal_rows=cal_rows, cal_year=cal_year, cal_month=cal_month,
        sym_data=sym_data, recent=recent,
        tv_sym=tv_sym, _tv=_tv,
        eq_labels=json.dumps(eq_labels), eq_values=json.dumps(eq_values),
        all_syms=all_syms, open_by_sym=open_by_sym, default_sym=default_sym)

# ── HTML ──────────────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gold Bot — Live Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
:root{
  --bg:#0a0e1a;--bg2:#111827;--bg3:#1a2235;--border:#1e293b;
  --text:#e2e8f0;--sub:#64748b;--green:#22c55e;--red:#ef4444;
  --blue:#3b82f6;--yellow:#f59e0b;--purple:#8b5cf6;
  --green-bg:rgba(34,197,94,.1);--red-bg:rgba(239,68,68,.1);
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;font-size:14px}
a{color:var(--blue);text-decoration:none}

/* ── Header ── */
.header{background:var(--bg2);border-bottom:1px solid var(--border);
        padding:12px 24px;display:flex;align-items:center;justify-content:space-between;
        position:sticky;top:0;z-index:100;backdrop-filter:blur(8px)}
.logo{display:flex;align-items:center;gap:10px}
.logo-icon{width:32px;height:32px;background:linear-gradient(135deg,#f59e0b,#ef4444);
           border-radius:8px;display:flex;align-items:center;justify-content:center;
           font-size:16px}
.logo-text{font-size:16px;font-weight:700;color:#fff}
.logo-sub{font-size:11px;color:var(--sub);margin-top:1px}
.header-right{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.badge{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;
       border-radius:20px;font-size:11px;font-weight:600;border:1px solid}
.badge-green{background:var(--green-bg);color:var(--green);border-color:rgba(34,197,94,.3)}
.badge-red  {background:var(--red-bg);  color:var(--red);  border-color:rgba(239,68,68,.3)}
.badge-gray {background:rgba(100,116,139,.1);color:var(--sub);border-color:var(--border)}
.dot{width:6px;height:6px;border-radius:50%;background:currentColor;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.btn-restart{padding:5px 14px;border-radius:20px;font-size:11px;font-weight:600;
             background:rgba(245,158,11,.1);color:var(--yellow);
             border:1px solid rgba(245,158,11,.3);cursor:pointer;transition:.2s}
.btn-restart:hover{background:rgba(245,158,11,.2)}

/* ── Layout ── */
.main{max-width:1440px;margin:0 auto;padding:20px 24px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}
.grid5{display:grid;grid-template-columns:repeat(5,1fr);gap:16px}
.mb{margin-bottom:16px}

/* ── Cards ── */
.card{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:20px;
      transition:border-color .2s}
.card:hover{border-color:#2d3f5a}
.card-title{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;
            color:var(--sub);margin-bottom:14px;display:flex;align-items:center;gap:6px}
.card-title svg{opacity:.6}
.kpi{font-size:28px;font-weight:700;line-height:1;color:#fff;margin-bottom:6px}
.kpi-sub{font-size:12px;color:var(--sub)}
.kpi-change{font-size:13px;font-weight:600;margin-top:4px}

/* ── Colors ── */
.green{color:var(--green)}.red{color:var(--red)}
.blue{color:var(--blue)}.yellow{color:var(--yellow)}.sub{color:var(--sub)}

/* ── Tables ── */
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:var(--bg3);color:var(--sub);font-weight:500;padding:10px 14px;
   text-align:left;border-bottom:1px solid var(--border);white-space:nowrap;font-size:11px;
   text-transform:uppercase;letter-spacing:.05em}
td{padding:11px 14px;border-bottom:1px solid var(--border);white-space:nowrap}
tr:last-child td{border-bottom:none}
tbody tr:hover td{background:var(--bg3)}
.pill{padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;letter-spacing:.02em}
.pill-green{background:var(--green-bg);color:var(--green)}
.pill-red  {background:var(--red-bg);  color:var(--red)}
.pill-blue {background:rgba(59,130,246,.1);color:var(--blue)}
.pill-gray {background:var(--bg3);color:var(--sub)}
.pill-yellow{background:rgba(245,158,11,.1);color:var(--yellow)}

/* ── P&L cell ── */
.pnl-pos{color:var(--green);font-weight:600}
.pnl-neg{color:var(--red);font-weight:600}
.pnl-zero{color:var(--sub)}

/* ── Progress bar ── */
.bar-wrap{width:60px;height:5px;background:var(--border);border-radius:3px;display:inline-block;vertical-align:middle}
.bar-fill{height:100%;border-radius:3px;background:var(--green)}

/* ── Calendar ── */
.cal{width:100%;border-collapse:collapse;font-size:12px}
.cal th{padding:6px;text-align:center;color:var(--sub);font-size:11px;font-weight:500;text-transform:uppercase}
.cal td{padding:8px 4px;text-align:center;border-radius:6px;line-height:1.3}
.cal td.empty{background:transparent}
.cal td.profit{background:rgba(34,197,94,.12);color:var(--green)}
.cal td.loss{background:rgba(239,68,68,.12);color:var(--red)}
.cal td:not(.empty):not(.profit):not(.loss){color:var(--sub)}
.cal-day{font-weight:600;font-size:12px}
.cal-pnl{font-size:10px;margin-top:2px;opacity:.8}

/* ── Symbol bars ── */
.sym-row{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.sym-label{width:90px;font-size:12px;color:var(--sub);flex-shrink:0;overflow:hidden;
           text-overflow:ellipsis;white-space:nowrap}
.sym-track{flex:1;background:var(--border);border-radius:4px;height:6px;overflow:hidden}
.sym-fill{height:100%;border-radius:4px;background:linear-gradient(90deg,var(--blue),var(--green))}
.sym-pct{width:36px;text-align:right;font-size:12px;color:var(--green);font-weight:600}
.sym-count{width:40px;text-align:right;font-size:11px;color:var(--sub)}

/* ── Live indicator ── */
.live-dot{display:inline-block;width:7px;height:7px;border-radius:50%;
          background:var(--green);margin-right:5px;animation:pulse 1.5s infinite}
.last-update{font-size:11px;color:var(--sub)}

/* ── Chart ── */
.chart-wrap{position:relative;height:220px}

/* ── Responsive ── */
@media(max-width:1100px){.grid5{grid-template-columns:repeat(3,1fr)}}
@media(max-width:800px) {.grid4{grid-template-columns:1fr 1fr}.grid3{grid-template-columns:1fr 1fr}.grid5{grid-template-columns:1fr 1fr}}
@media(max-width:560px) {.grid4,.grid3,.grid2,.grid5{grid-template-columns:1fr}.header{flex-direction:column;gap:10px;align-items:flex-start}}

/* ── Divider ── */
.section-label{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;
               color:var(--sub);padding:6px 0 10px;border-top:1px solid var(--border);margin-top:4px}
</style>
</head>
<body>

<!-- ══ HEADER ══════════════════════════════════════════════════════════════ -->
<div class="header">
  <div class="logo">
    <div class="logo-icon">💰</div>
    <div>
      <div class="logo-text">GoldBot Dashboard</div>
      <div class="logo-sub">NautilusTrader · FxPro MT5</div>
    </div>
  </div>
  <div class="header-right">
    {% if bot_running %}
      <span class="badge badge-green"><span class="dot"></span>Bot Running</span>
    {% else %}
      <span class="badge badge-red"><span class="dot"></span>Bot Stopped</span>
    {% endif %}
    {% if mt5_ok %}
      <span class="badge badge-green"><span class="dot"></span>MT5 Connected</span>
    {% else %}
      <span class="badge badge-red">MT5 Disconnected</span>
    {% endif %}
    <span class="badge badge-gray" id="lastUpdate">Updated: {{ last_scan[:19] if last_scan != '—' else '—' }}</span>
    <form method="POST" action="/restart" id="restartForm" style="display:inline">
      <input type="hidden" name="pwd" id="restartPwd">
      <button type="button" class="btn-restart" onclick="doRestart()">⟳ Restart Bot</button>
    </form>
  </div>
</div>

<!-- ══ MAIN ═════════════════════════════════════════════════════════════════ -->
<div class="main">

  <!-- ── KPI row ── -->
  <div class="grid5 mb">

    <div class="card">
      <div class="card-title">Balance (JPY)</div>
      <div class="kpi" id="kpi-balance">¥{{ "{:,.0f}".format(balance) }}</div>
      <div class="kpi-sub">Account cash</div>
    </div>

    <div class="card">
      <div class="card-title">Equity (JPY)</div>
      <div class="kpi {% if equity >= balance %}green{% else %}red{% endif %}" id="kpi-equity">
        ¥{{ "{:,.0f}".format(equity) }}
      </div>
      <div class="kpi-sub {% if total_unrealized >= 0 %}green{% else %}red{% endif %}">
        {% if total_unrealized >= 0 %}+{% endif %}{{ "{:.2f}".format(total_unrealized) }} unrealised
      </div>
    </div>

    <div class="card">
      <div class="card-title">Win Rate</div>
      <div class="kpi {% if win_rate >= 50 %}green{% elif win_rate > 0 %}yellow{% else %}sub{% endif %}">
        {{ win_rate }}%
      </div>
      <div class="kpi-sub">{{ total_wins }}/{{ total_trades }} trades</div>
    </div>

    <div class="card">
      <div class="card-title">Today's P&amp;L</div>
      <div class="kpi {% if today_profit > 0 %}green{% elif today_profit < 0 %}red{% else %}sub{% endif %}">
        {% if today_profit > 0 %}+{% endif %}{{ "{:.2f}".format(today_profit) }}
      </div>
      <div class="kpi-sub">{{ today_count }} closed today</div>
    </div>

    <div class="card">
      <div class="card-title">Total P&amp;L</div>
      <div class="kpi {% if total_profit > 0 %}green{% elif total_profit < 0 %}red{% else %}sub{% endif %}">
        {% if total_profit > 0 %}+{% endif %}{{ "{:.2f}".format(total_profit) }}
      </div>
      <div class="kpi-sub">All closed trades</div>
    </div>

  </div>

  <!-- ── Open positions ── -->
  {% if open_pos %}
  <div class="section-label">Open Positions ({{ open_pos|length }})</div>
  <div class="card mb">
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>Ticket</th><th>Symbol</th><th>Side</th>
        <th>Entry</th><th>Current</th><th>Volume</th>
        <th>Stop Loss</th><th>Take Profit</th><th>Progress</th><th>Unrealised P&L</th>
      </tr></thead>
      <tbody id="positions-body">
      {% for p in open_pos %}
      {% set tp_pct = [(((p.current_price - p.entry_price) / ((p.tp or p.entry_price) - p.entry_price) * 100) if (p.tp and p.tp != p.entry_price) else 0), 0]|max %}
      <tr>
        <td class="blue" style="font-family:monospace">{{ p.ticket }}</td>
        <td style="font-weight:600">{{ p.symbol }}</td>
        <td><span class="pill {% if p.direction=='BUY' %}pill-green{% else %}pill-red{% endif %}">{{ p.direction }}</span></td>
        <td>{{ "{:.2f}".format(p.entry_price) }}</td>
        <td style="font-weight:600">{{ "{:.2f}".format(p.current_price) }}</td>
        <td class="sub">{{ p.volume }}</td>
        <td class="red">{{ "{:.2f}".format(p.sl) if p.sl is not none else "—" }}</td>
        <td class="green">{{ "{:.2f}".format(p.tp) if p.tp is not none else "—" }}</td>
        <td>
          <div class="bar-wrap"><div class="bar-fill" style="width:{{ [tp_pct, 100]|min }}%"></div></div>
        </td>
        <td class="{% if p.profit > 0 %}pnl-pos{% elif p.profit < 0 %}pnl-neg{% else %}pnl-zero{% endif %}">
          {% if p.profit > 0 %}+{% endif %}{{ "{:.2f}".format(p.profit) }}
        </td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
  </div>
  {% endif %}

  <!-- ── Charts + Calendar ── -->
  <div class="section-label">Performance</div>
  <div class="grid3 mb">

    <div class="card" style="grid-column:span 2">
      <div class="card-title">Cumulative P&L Curve</div>
      <div class="chart-wrap">
        <canvas id="equityChart"></canvas>
      </div>
    </div>

    <div class="card">
      <div class="card-title">Monthly P&L — {{ cal_year }}/{{ "%02d"|format(cal_month) }}</div>
      <table class="cal">
        <tr>{% for d in ["M","T","W","T","F","S","S"] %}<th>{{ d }}</th>{% endfor %}</tr>
        {% for week in cal_rows %}
        <tr>
          {% for cell in week %}
          <td class="{{ cell.cls }}">
            {% if cell.day %}
              <div class="cal-day">{{ cell.day }}</div>
              {% if cell.pnl is not none %}
                <div class="cal-pnl">{% if cell.pnl>=0 %}+{% endif %}{{ "{:.0f}".format(cell.pnl) }}</div>
              {% endif %}
            {% endif %}
          </td>
          {% endfor %}
        </tr>
        {% endfor %}
      </table>
    </div>

  </div>

  <!-- ── Symbol stats + Today ── -->
  <div class="grid2 mb">

    <div class="card">
      <div class="card-title">Win Rate by Symbol</div>
      {% if sym_data %}
        {% for s in sym_data %}
        <div class="sym-row">
          <span class="sym-label" title="{{ s.symbol }}">{{ s.symbol }}</span>
          <div class="sym-track"><div class="sym-fill" style="width:{{ s.rate }}%"></div></div>
          <span class="sym-pct">{{ s.rate }}%</span>
          <span class="sym-count">{{ s.wins }}/{{ s.total }}</span>
        </div>
        {% endfor %}
      {% else %}
        <p class="sub" style="padding:20px 0;text-align:center">No closed trades yet.</p>
      {% endif %}
    </div>

    <div class="card">
      <div class="card-title">Today's Summary</div>
      <div class="grid2" style="gap:12px">
        <div style="background:var(--bg3);border-radius:8px;padding:16px">
          <div style="font-size:11px;color:var(--sub);margin-bottom:6px">TRADES</div>
          <div style="font-size:24px;font-weight:700;color:var(--blue)">{{ today_count }}</div>
        </div>
        <div style="background:var(--bg3);border-radius:8px;padding:16px">
          <div style="font-size:11px;color:var(--sub);margin-bottom:6px">NET P&L</div>
          <div style="font-size:24px;font-weight:700" class="{% if today_profit>0 %}green{% elif today_profit<0 %}red{% else %}sub{% endif %}">
            {% if today_profit>0 %}+{% endif %}{{ "{:.2f}".format(today_profit) }}
          </div>
        </div>
        <div style="background:var(--bg3);border-radius:8px;padding:16px">
          <div style="font-size:11px;color:var(--sub);margin-bottom:6px">BEST</div>
          <div style="font-size:22px;font-weight:700;color:var(--green)">+{{ "{:.2f}".format(best_today) }}</div>
        </div>
        <div style="background:var(--bg3);border-radius:8px;padding:16px">
          <div style="font-size:11px;color:var(--sub);margin-bottom:6px">WORST</div>
          <div style="font-size:22px;font-weight:700;color:var(--red)">{{ "{:.2f}".format(worst_today) }}</div>
        </div>
      </div>
    </div>

  </div>

  <!-- ── TradingView chart ── -->
  <div class="section-label">Price Chart — Currently Trading</div>
  <div class="card mb">
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px">
      {% for sym in all_syms %}
        {% set cnt = open_by_sym.get(sym, 0) %}
        {% set is_active = (sym == default_sym) %}
        <button onclick="loadChart('{{ _tv.get(sym,sym) }}','{{ sym }}',this)"
                style="display:inline-flex;align-items:center;gap:6px;
                       padding:5px 12px;border-radius:8px;font-size:12px;font-weight:600;
                       background:{% if is_active %}rgba(59,130,246,.2){% elif cnt > 0 %}rgba(34,197,94,.1){% else %}var(--bg3){% endif %};
                       color:{% if is_active %}var(--blue){% elif cnt > 0 %}var(--green){% else %}var(--sub){% endif %};
                       border:1px solid {% if is_active %}rgba(59,130,246,.4){% elif cnt > 0 %}rgba(34,197,94,.3){% else %}var(--border){% endif %};
                       cursor:pointer;transition:.2s" class="chart-btn">
          {{ sym }}
          {% if cnt > 0 %}
            <span style="background:{% if is_active %}var(--blue){% else %}var(--green){% endif %};
                         color:#fff;border-radius:10px;padding:0 6px;font-size:10px;font-weight:700">
              {{ cnt }} open
            </span>
          {% endif %}
        </button>
      {% endfor %}
    </div>
    <div id="tv_chart" style="width:100%;height:420px"></div>
  </div>

  <!-- ── Recent trades ── -->
  <div class="section-label">Recent Closed Trades</div>
  <div class="card">
    {% if recent %}
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>Ticket</th><th>Symbol</th><th>Side</th>
        <th>Open</th><th>Close</th><th>Entry</th><th>Exit</th>
        <th>Lots</th><th>P&L</th><th>Reason</th><th>R:R</th>
      </tr></thead>
      <tbody>
      {% for t in recent %}
      <tr>
        <td class="blue" style="font-family:monospace">{{ t.ticket }}</td>
        <td style="font-weight:600">{{ t.symbol }}</td>
        <td><span class="pill {% if t.direction=='BUY' %}pill-green{% else %}pill-red{% endif %}">{{ t.direction }}</span></td>
        <td class="sub">{{ (t.open_time or "")[:16] }}</td>
        <td class="sub">{{ (t.close_time or "")[:16] }}</td>
        <td>{{ "{:.2f}".format(t.entry_price or 0) }}</td>
        <td>{{ "{:.2f}".format(t.exit_price) if t.exit_price else "—" }}</td>
        <td class="sub">{{ t.lot_size }}</td>
        <td class="{% if (t.profit or 0)>0 %}pnl-pos{% elif (t.profit or 0)<0 %}pnl-neg{% else %}pnl-zero{% endif %}">
          {% if t.profit is not none %}{% if t.profit>0 %}+{% endif %}{{ "{:.2f}".format(t.profit) }}{% else %}—{% endif %}
        </td>
        <td>
          {% if t.exit_reason %}
          <span class="pill {% if t.exit_reason=='TP' %}pill-green{% elif t.exit_reason=='SL' %}pill-red{% elif t.exit_reason=='EMA' %}pill-blue{% else %}pill-gray{% endif %}">
            {{ t.exit_reason }}
          </span>
          {% else %}—{% endif %}
        </td>
        <td class="sub">{{ "{:.2f}".format(t.rr_ratio) if t.rr_ratio else "—" }}</td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
    {% else %}
      <p class="sub" style="padding:30px;text-align:center">No closed trades yet — positions will appear here after they close.</p>
    {% endif %}
  </div>

</div><!-- .main -->

<!-- ══ SCRIPTS ══════════════════════════════════════════════════════════════ -->
<script>
// ── Equity chart ──────────────────────────────────────────────────────────
const eqLabels = {{ eq_labels|safe }};
const eqValues = {{ eq_values|safe }};
if (eqLabels.length > 0) {
  const ctx = document.getElementById('equityChart').getContext('2d');
  const grad = ctx.createLinearGradient(0, 0, 0, 220);
  grad.addColorStop(0, 'rgba(34,197,94,.25)');
  grad.addColorStop(1, 'rgba(34,197,94,.02)');
  new Chart(ctx, {
    type: 'line',
    data: {
      labels: eqLabels,
      datasets: [{
        label: 'Cumulative P&L',
        data: eqValues,
        borderColor: '#22c55e',
        backgroundColor: grad,
        borderWidth: 2,
        pointRadius: eqLabels.length < 30 ? 4 : 0,
        pointHoverRadius: 5,
        fill: true,
        tension: 0.4,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false },
                 tooltip: { mode: 'index', intersect: false,
                            callbacks: { label: c => ' P&L: ' + c.raw.toFixed(2) } } },
      scales: {
        x: { grid: { color: '#1e293b' }, ticks: { color: '#64748b', maxTicksLimit: 8 } },
        y: { grid: { color: '#1e293b' }, ticks: { color: '#64748b',
             callback: v => v >= 0 ? '+' + v.toFixed(0) : v.toFixed(0) } }
      }
    }
  });
} else {
  document.getElementById('equityChart').parentElement.innerHTML =
    '<p style="color:#64748b;text-align:center;padding:80px 0">Equity curve will appear after first closed trade.</p>';
}

// ── TradingView chart ─────────────────────────────────────────────────────
function loadChart(tvSym, label, btn) {
  // Highlight active button
  document.querySelectorAll('.chart-btn').forEach(b => {
    b.style.background = b.dataset.hasOpen === '1'
      ? 'rgba(34,197,94,.1)' : 'var(--bg3)';
    b.style.color = b.dataset.hasOpen === '1' ? 'var(--green)' : 'var(--sub)';
    b.style.borderColor = b.dataset.hasOpen === '1'
      ? 'rgba(34,197,94,.3)' : 'var(--border)';
  });
  if (btn) {
    btn.style.background = 'rgba(59,130,246,.2)';
    btn.style.color = 'var(--blue)';
    btn.style.borderColor = 'rgba(59,130,246,.4)';
  }
  const el = document.getElementById('tv_chart');
  el.innerHTML = '';
  const qs = new URLSearchParams({
    symbol: tvSym, interval: '15', timezone: 'Etc/UTC',
    theme: 'dark', style: '1', locale: 'en',
    toolbar_bg: '#111827', enable_publishing: 'false',
    hide_top_toolbar: 'false', save_image: 'false',
    container_id: 'tv_chart',
  });
  const f = document.createElement('iframe');
  f.setAttribute('allowtransparency','true');
  f.setAttribute('frameborder','0');
  f.style.cssText = 'width:100%;height:420px;border:none;border-radius:6px';
  f.src = 'https://s.tradingview.com/widgetembed/?' + qs;
  el.appendChild(f);
}
// Tag buttons with open-position info for highlight reset
document.querySelectorAll('.chart-btn').forEach(btn => {
  btn.dataset.hasOpen = btn.textContent.includes('open') ? '1' : '0';
});
loadChart('{{ tv_sym }}', '{{ default_sym }}');

// ── Live refresh every 15 s (positions + balance only, no full reload) ────
function refreshPositions() {
  fetch('/api/state')
    .then(r => r.json())
    .then(d => {
      // Balance
      document.getElementById('kpi-balance').textContent =
        '¥' + d.balance.toLocaleString('en-US', {maximumFractionDigits:0});
      // Last update
      if (d.last_scan_time) {
        document.getElementById('lastUpdate').textContent =
          'Updated: ' + d.last_scan_time.substring(0,19);
      }
      // Positions table
      const tbody = document.getElementById('positions-body');
      if (!tbody) return;
      tbody.innerHTML = (d.open_positions || []).map(p => {
        const dir   = p.direction === 'BUY';
        const pnl   = p.profit || 0;
        const pnlCls= pnl > 0 ? 'pnl-pos' : pnl < 0 ? 'pnl-neg' : 'pnl-zero';
        const pnlStr= (pnl >= 0 ? '+' : '') + pnl.toFixed(2);
        const tpPct = p.tp && p.tp !== p.entry_price
          ? Math.min(100, Math.max(0,
              (p.current_price - p.entry_price) / (p.tp - p.entry_price) * 100))
          : 0;
        return `<tr>
          <td class="blue" style="font-family:monospace">${p.ticket}</td>
          <td style="font-weight:600">${p.symbol}</td>
          <td><span class="pill ${dir?'pill-green':'pill-red'}">${p.direction}</span></td>
          <td>${p.entry_price.toFixed(2)}</td>
          <td style="font-weight:600">${p.current_price.toFixed(2)}</td>
          <td class="sub">${p.volume}</td>
          <td class="red">${p.sl != null ? p.sl.toFixed(2) : '—'}</td>
          <td class="green">${p.tp != null ? p.tp.toFixed(2) : '—'}</td>
          <td><div class="bar-wrap"><div class="bar-fill" style="width:${tpPct.toFixed(0)}%"></div></div></td>
          <td class="${pnlCls}">${pnlStr}</td>
        </tr>`;
      }).join('');
    })
    .catch(() => {});
}
setInterval(refreshPositions, 15000);

// ── Restart password prompt ───────────────────────────────────────────────
function doRestart() {
  const p = prompt('Enter restart password:');
  if (p === null) return;
  document.getElementById('restartPwd').value = p;
  document.getElementById('restartForm').submit();
}
</script>
</body>
</html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Dashboard → http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
