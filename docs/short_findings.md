# Short-sell strategy — findings

Investigation of a short-side system for newgold (NautilusTrader 1.231 + MT5
FxPro demo), built separately from the live long-only `EMACross`. This summarizes
what was built, what was verified, and what the backtests actually showed.

Date: 2026-10-19. Branch: `short-strategy`.

All P&L below is at **0.01 lot** (minimum), in **USD**. GOLD uses the corrected
contract multiplier (×100); index/BTC CFDs are contract 1 (correct); **WTI P&L is
×1000 understated** because the Nautilus `Cfd` class hardcodes multiplier=1
(win%/sign are still valid). Backtests are GOLD/CFD **H1** bars from the local MT5
terminal.

---

## 1. What was built

| file | purpose |
|---|---|
| `strategy/short_ema_cross.py` | S1-only short control — sign-flipped mirror of the live `EMACross` |
| `strategy/multi_short.py` | multi-detector engine: S1/S2/S4 + optional regime filter + configurable exits |
| `strategy/risk_sizing.py` | JPY risk sizing via `order_calc_profit` (pure math + mt5 probe) |
| `adapters/mt5/execution.py` (additive) | `modify_position_sltp` — venue-side SL/TP via `TRADE_ACTION_SLTP` |
| `adapters/mt5/instrument.py` (additive) | optional `multiplier` arg on `build_gold_instrument` (default 1 → live unchanged) |
| `pull_all_h1.py`, `build_catalog_multi.py` | pull H1 for all 6 instruments → `catalog_multi/` |
| `run_backtest_short.py`, `run_backtest_multi.py`, `run_backtest_multi_all.py`, `run_exit_variants.py` | backtest/diagnostic runners |
| `tests/test_sltp_modify.py`, `test_sltp_integration.py`, `test_risk_sizing.py`, `test_short_live_check.py` | demo-verified tests |

The live long-only system was never modified. The two edits to live adapter files
are additive with defaults that preserve existing behavior.

### Detectors
- **S1** — EMA death cross: prev fast≥slow, this fast<slow → short.
- **S2** — pullback/resumption: downtrend (fast<slow) + a prior bar's high reached
  slow_ema − 0.25·ATR, then a later bar closes below the fast EMA → short.
- **S4** — Donchian breakdown: downtrend + close < min(low) of the prior 20 bars → short.

Shared: ATR bracket (SL above / TP below), golden-cross regime exit, position
limits, optional JPY risk sizing.

---

## 2. Mechanics — verified, bug-free

Every backtest run passed automated bug checks (SELL opens == BUY closes, no
orphans, P&L sign matches short direction, nothing left open). The engine was
exercised across a `CurrencyPair` (GOLD) and five `Cfd` instruments, price scales
$70 (WTI) to $40k (BTC), bull and bear regimes.

Live (FxPro demo) verification:
- **SL/TP modify** (`TRADE_ACTION_SLTP`) — isolated + through the real async client.
- **JPY risk sizing** — agrees with MT5's `order_calc_profit` within ~0.06%.
- **Real short lifecycle** — SELL-to-open + reduce-only BUY-to-close, with a short
  bracket (SL above, TP below) accepted by FxPro, closed cleanly, no orphan.

Note: FxPro reports `trade_stops_level=0` but still enforces a dynamic freeze
distance — a too-tight SL is rejected `10016 INVALID_STOPS`; the adapter treats
that as benign (keep prior stop).

---

## 3. Backtest results

### 3.1 All 6 instruments, 2021–2025 (combined S1+S2+S4, 0.01 lot)

| instrument | trades | win% | net P&L |
|---|---|---|---|
| GOLD | 1,349 | 37.6% | −$1,137 |
| #USNDAQ100 | 1,329 | 37.5% | −$278 |
| #USSPX500 | 1,307 | 35.9% | −$65 |
| WTI | 1,398 | 26.4% | −$1.82 (×1000 → ~−$1,820) |
| BITCOIN | 1,930 | 38.4% | −$1,209 |
| #Japan225 | 1,446 | 35.3% | −$448 |

**Net negative on every instrument.** 2021–2025 was broadly bullish (equities,
gold, crypto), so short-only bleeds — expected, not a bug.

### 3.2 Bear window (2022) — where shorts should work

Most instruments still lost (equity declines were choppy with violent bear
rallies that whipsawed the entries). The exception:

| cell | trades | win% | P&L/trade | net |
|---|---|---|---|---|
| **BITCOIN S4** (default exits) | 224 | 44.2% | +0.644 | +$144 |
| **BITCOIN S4** (pure 1:2 bracket) | 217 | 39.6% | +1.282 | +$278 |

BITCOIN fell ~65% in a clean, sustained trend — the one regime/instrument where
the momentum-breakdown short had a clear edge.

### 3.3 S4's edge is regime-locked (not robust)

| window | BITCOIN S4, P&L/trade (pure bracket) |
|---|---|
| 2022 (bear) | **+1.28** |
| 2021–2025 (mixed) | −0.42 |
| 2024–2025 (bull) | −0.21 |

The strong 2023–2026 and 2022 showings do not generalize; S4 is only profitable in
a genuine downtrend.

---

## 4. Exit logic — the main tuning lesson

The default exits (move SL to breakeven at 1R, then trail) were a **net-negative
leak**: they clip winners below the loss size. On #USNDAQ100 S1 in 2022 the payoff
ratio (avg win ÷ avg loss) was only **0.90** despite a 1:2 bracket — which is why a
~50%-win strategy still lost.

Switching to a **pure 1:2 bracket** (no breakeven, no trail) was validated across
bear/mixed/bull windows (`run_exit_variants.py`):

- **Payoff ratio improved in all 12 cell-windows tested** (100% consistent).
- P&L/trade better-or-neutral in 10/12 (the two exceptions are <0.01 noise).
- It does **not** backfire in bull/mixed regimes (the original worry).
- One real exception: S1 death-cross entries on BTC in a strong bear preferred the
  trail (BTC S1 2022: +0.12 → −0.11).

**Recommendation:** make the pure 1:2 bracket the default exit
(`breakeven_r=1e9, trail_sl=False`); optionally keep the trail for S1 death-cross
entries in strong bears.

But: **better exits improved the numbers, they did not create an edge.** Shorting
remained net-negative in every non-bear window even with the pure bracket.

---

## 5. Things that did NOT work

- **Simple regime/trend-strength filter** (require slow EMA falling 50 bars +
  price below it) — *hurt* performance. It entered late, deleting the early
  clean-trend shorts and keeping late squeeze-prone ones. BITCOIN combined went
  +0.15 → −0.03. A naive entry-side trend gate is not the fix.
- **S2 (pullback/resumption)** — negative in essentially every test.
- **Shorting GOLD** — negative across 2008–2026, 2021–2025, and 2022; gold's
  secular bull makes it the wrong instrument to judge shorts on.

---

## 6. Contract-multiplier note (P&L correctness)

Nautilus P&L = `quantity × multiplier × price`. FxPro contract sizes: GOLD 100,
WTI 1000, indices/BTC 1.

- **GOLD** fixed for backtests via `build_gold_instrument(multiplier=100)` (default
  stays 1 so the live long system is unchanged).
- **WTI** cannot be fixed the same way — the Nautilus `Cfd` class hardcodes
  multiplier=1, so WTI backtest P&L is ×1000 understated. Win%/sign are unaffected.
- The **live dashboard** still shows GOLD P&L ×100-small for the long system; a
  global fix would change the running system's numbers and is deferred.

---

## 7. Conclusions

1. **Mechanics are production-grade and live-verified** for shorts (entry, short
   bracket, reduce-only close, SL/TP modify, JPY sizing).
2. **The pure 1:2 bracket is the right default exit** — validated robust across
   regimes.
3. **There is no standalone short edge in rising/choppy markets.** The only
   profitable pocket found anywhere is **S4 (Donchian breakdown) on BITCOIN in a
   genuine downtrend** — regime-dependent, not all-weather.
4. Shorting is a **bear-regime instrument**; deploying it unconditionally in a bull
   market loses money.

---

## 8. Not done / deployment gates

- Not wired to a live runner (no `run_live_short.py` / watchdog entry).
- `reconciliation=False` → must start flat (restart with an open short orphans it).
- No swap/overnight, news-blackout, or correlation policy
  (#USNDAQ100/#USSPX500/#Japan225 ≈ one bet).
- Risk sizing module exists but the per-symbol JPY constants aren't wired into a
  live runner yet.
- Any deployment should realistically be **S4-on-crypto only**, started flat, with
  the pure-bracket exit.

---

*Reproduce:* `pull_all_h1.py` → `build_catalog_multi.py` →
`run_backtest_multi_all.py --start … --end …` and
`run_exit_variants.py --start … --end …`.
