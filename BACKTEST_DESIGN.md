# Backtest Runner — Design Document

## Plain-English Summary

The backtest runner replays historical Binance OHLCV data **bar-by-bar** through
the **existing v2 signal engine** — the exact same `compute_signal()` and
`build_risk_plan()` functions used in live forward-testing — and records paper
trades into the **same `trades` table** as live trades, tagged with a
`source = 'backtest'` column so the two never mix.

It is **research only**. It does not replace the live forward-test. Its job is to
quickly generate many trades across symbols/regimes, expose which symbols and
regimes the v2 engine works on, and seed hypotheses for Phase C (regime-aware
slippage, derivatives overlay, filters).

### The single most important rule: **no look-ahead**

At bar `t` the runner only sees data up to and including bar `t`. Signal
decisions are made on the **close** of bar `t`, and trades are entered and
managed on **bar `t+1` onward**. This mirrors how the live system behaves — a
signal computed from today's closed candle becomes actionable at the *next*
candle's open.

### How it reuses existing modules (zero logic changes)

| Module | Used for | Changed? |
|--------|----------|----------|
| `market_data.py` | Historical OHLCV fetch (reusable `_get_ohlcv_binance`) | No (add a paginated loader helper) |
| `indicators.py` | `compute_all(df)` on rolling windows | No |
| `patterns.py` | `latest(df)` per-bar pattern flags + strengths | No |
| `signals.py` | `compute_signal()` — the frozen v2 logic | No |
| `risk.py` | `build_risk_plan()` — structural stops / vol TP | No |
| `service.py` | `_r_multiple()`, `_net_r()`, `_r_multiple` sizing math | No (import values, don't edit) |
| `store.py` | `trades` schema, fee/slippage constants path | Add `source` column migration only |

The runner imports `_net_r` and `_r_multiple` directly from `service` (they are
module-level pure functions), so all cost math is byte-for-byte identical to live.

---

## Data Flow

```
1. Parse CLI args (symbols, interval, start, end, equity, risk%, output dir)
2. For each symbol:
     a. Load full OHLCV history (paginated Binance klines across date range)
     b. Slice to [start, end]
     c. Warm-up: ensure enough leading bars for EMA(200), ATR(14), pivots
     d. Iterate bar-by-bar from warm-up end:
          - grow window: df[:t+1]
          - compute_all(df[:t+1]) → ind
          - patterns.latest(df[:t+1]) → merge into ind
          - compute_signal(symbol, ind, market, df=df[:t+1]) → sig
          - if direction != NEUTRAL and no position open:
                open position using risk sizing (enter NEXT bar open)
3. Manage open positions per-bar:
     - if stop/target touched on the current bar's High/Low → exit AT the level
     - SL checked before TP within the same bar (conservative, matches replay_missed_exits)
     - when position closes, compute r_multiple + net_r on the exit level
4. Write trades to store (source='backtest') and/or CSV
5. Emit summary report (markdown)
```

---

## Bar-by-Bar Loop and Look-Ahead Avoidance

### Window growth (the safe pattern)

Rather than re-fetching per bar, we load the whole series once (fast) and then
**grow a view** `df.iloc[:i+1]`. `compute_all`, `patterns.latest`, and
`build_risk_plan` all read **only the tail** of the passed DataFrame, so passing
the prefix window is safe and enforces point-in-time semantics.

```python
# The window passed to the engine is strictly prefix-limited.
window = df.iloc[:i + 1]
ind = compute_all(window)
ind.update(patterns.latest(window))
sig = compute_signal(symbol, ind, market=market, df=window, engine_version="v2")
```

### Signal → Entry timing

A signal generated on close of bar `i` is entered at the **open of bar `i+1`**.
A non-NEUTRAL signal is only actionable if no position is currently open for
that symbol (single-position-per-symbol, matching live `MAX_ACTIVE_TRADES` and
queue discipline).

```python
if position is None and sig.direction != "NEUTRAL":
    # Enter at next bar's OPEN — never the signal bar's close.
    entry_bar = i + 1
    if entry_bar < len(df):
        entry = float(df["Open"].iloc[entry_bar])
        position = open_position(sig, entry, entry_bar)
```

Note: entry is the *next open*, and the risk bracket (`stop_loss`, `take_profit`)
is recomputed via `build_risk_plan` at that entry point so the levels are
consistent with the actual fill (not the signal snapshot's pseudo-close levels).
This matches live behavior where risk is derived from structure at execution.

### Bar-by-bar trade management (no look-ahead)

Once a position is open, advance one bar at a time. On each new bar `i`:

```python
o, h, l, c = df.iloc[i]  # this bar's OHLC
if position.direction == "LONG":
    if l <= position.stop_loss:
        exit_level, status = position.stop_loss, "SL_HIT"   # SL checked first
    elif h >= position.take_profit:
        exit_level, status = position.take_profit, "TP_HIT"
elif position.direction == "SHORT":
    if h >= position.stop_loss:
        exit_level, status = position.stop_loss, "SL_HIT"
    elif l <= position.take_profit:
        exit_level, status = position.take_profit, "TP_HIT"
```

If neither SL nor TP is touched on bar `i`, the position carries forward. A
manual timeout close is optional (e.g., after N bars), but for research fidelity
we default to SL/TP-only exits (consistent with live). SL-before-TP ordering
within a bar is the same conservative rule `service.replay_missed_exits` uses.

---

## Trade Logging (same schema as live)

Reuse the exact `trades` table. Add one column:

```sql
ALTER TABLE trades ADD COLUMN source TEXT NOT NULL DEFAULT 'live';
```

All fields already present in live trades are written identically:

| Field | Backtest meaning |
|-------|------------------|
| `opened_at` | ISO timestamp of the *entry bar* open time |
| `closed_at` | ISO timestamp of the *exit bar* |
| `symbol` / `interval` / `direction` | from signal |
| `entry` | actual next-bar open (fill proxy) |
| `stop_loss` / `take_profit` | risk plan at entry |
| `size_quote` | risk_amount / stop_distance_pct (fixed equity) |
| `risk_quote` | equity × risk% |
| `status` | `SL_HIT` / `TP_HIT` / `MANUAL_CLOSE` |
| `exit_price` | the SL/TP level hit |
| `r_multiple` | `service._r_multiple(...)` |
| `net_r_score` | `service._net_r(...)` |
| `entry_slippage` / `exit_slippage` / `fee_paid` | flat `SLIPPAGE_BUFFER` + `active_taker_fee()` |
| `source` | `'backtest'` |

This means `regime_analysis.py`, the scorecard, and any downstream report can
run unchanged (adding `WHERE source='backtest'` where relevant).

---

## Outputs

1. **CSV** — `backtest_trades.csv` with exactly the `trades` columns (plus
   `source='backtest'`), optionally only the closed subset.
2. **Summary report (markdown)** — `backtest_report.md`:
   - Total closed trades, wins, losses, win rate, expectancy (R), profit factor,
     worst streak.
   - Breakdown by symbol, by direction, and by ATR%-ile regime bucket
     (high / mid / low) using `risk_metadata.atr_percentile` if present.
3. DB rows are inserted with `source='backtest'` so they can be:
   - Re-fed into `regime_analysis.py` via a `--source backtest` filter, or
   - Excluded from the official live n≥50 count via `WHERE source='live'`.

---

## Files

| File | Purpose |
|------|---------|
| `crypto/backtest.py` | The runner + CLI entry point. |
| `crypto/market_data.py` | Add `get_ohlcv_range()` (paginated loader) — small helper. |
| `crypto/store.py` | Add `source` schema migration + `save_backtest_trades()` + `list_trades(source=...)`. |
| `pyproject.toml` | Add `crypto-backtest` script entry. |

---

## CLI

```bash
uv run python -m crypto.backtest \
  --symbols BTC,ETH,SOL \
  --interval 1h \
  --start 2025-01-01 --end 2025-12-31 \
  --equity 10000 --risk-pct 1.0 \
  --out backtest_results
```

See `crypto/backtest.py` docstring for full options.
