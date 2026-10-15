“Approved design – do not implement until n ≥ 50 v2 trades.”

Here's the complete dual-track design.

---

## 1. ARCHITECTURE: TWO INDEPENDENT FEEDS FROM ONE SIGNAL ENGINE

```
                    ┌──────────────────────┐
                    │   CoinGecko Universe  │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │   Binance OHLCV      │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │  indicators + signals │  ← UNCHANGED, frozen at v2
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
     ┌────────▼────────┐  ┌───▼────────┐  ┌────▼─────┐
     │   TRACK A        │  │  TRACK B    │  │ Dashboard│
     │   Paper universe │  │  Micro-live │  │  + score │
     │   All symbols    │  │  BTC, ETH   │  │  cards   │
     │   Primary alpha  │  │  Exec drag  │  │          │
     └─────────────────┘  └────────────┘  └──────────┘

     One signal → one paper trade (Track A)
               → one live order (Track B, if eligible)
```

**The cardinal rule:** The signal engine produces ONE output per symbol per scan. Track A and Track B both consume that same output. Neither track modifies the signal. The only difference is what happens *after* the signal is computed.

---

## 2. DATA SEPARATION: THE WALL

### 2a. Schema changes to `trades` table

```sql
ALTER TABLE trades ADD COLUMN track TEXT NOT NULL DEFAULT 'A';
ALTER TABLE trades ADD COLUMN live_order_id TEXT;
ALTER TABLE trades ADD COLUMN live_fill_price REAL;
ALTER TABLE trades ADD COLUMN live_fill_ts TEXT;
ALTER TABLE trades ADD COLUMN live_slippage_actual REAL;
ALTER TABLE trades ADD COLUMN live_fee_actual REAL;
ALTER TABLE trades ADD COLUMN live_exchange TEXT;
ALTER TABLE trades ADD COLUMN live_order_type TEXT;
ALTER TABLE trades ADD COLUMN live_account_id TEXT;
```

### 2b. The wall rules

| Rule | Track A (Paper) | Track B (Micro-live) |
|------|----------------|---------------------|
| **Symbols** | All scan symbols | BTC, ETH only |
| **Max concurrent** | 5 (existing `MAX_ACTIVE_TRADES`) | 2 (hardcoded) |
| **Max per trade** | `risk_amount / stop_distance_pct` (existing) | 1% of account balance |
| **Execution** | Simulated (SL/TP hit from candle data) | Real order on exchange |
| **Fill data** | `entry` (signal price), `exit_price` (SL/TP level) | `live_fill_price` (actual), `live_fill_ts` (actual) |
| **Slippage** | Flat `SLIPPAGE_BUFFER` (0.05%) | `live_slippage_actual` (measured) |
| **Fees** | `active_taker_fee()` (estimated) | `live_fee_actual` (from exchange) |
| **Scorecard** | Included in primary alpha scorecard | **Excluded** from alpha scorecard |
| **Why exists** | Alpha estimation, strategy validation | Execution calibration only |

### 2c. Store functions

```python
# store.py — new functions

def open_trade_b(payload: dict[str, Any]) -> dict[str, Any]:
    """Open a Track B (micro-live) trade.
    
    Same schema as open_trade but with track='B' and live_* fields.
    Enforces max 2 concurrent Track B trades.
    """
    with _lock:
        conn = _connect()
        active_b = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status='OPEN' AND track='B'"
        ).fetchone()[0]
        if active_b >= 2:
            raise ValueError(f"Max 2 concurrent Track B trades (currently {active_b})")
        
        cur = conn.execute(
            """
            INSERT INTO trades (
                opened_at, symbol, interval, direction, entry, stop_loss,
                take_profit, size_quote, risk_quote, track
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'B')
            """,
            (
                _utcnow(), payload["symbol"], payload["interval"],
                payload["direction"], payload["entry"], payload["stop_loss"],
                payload["take_profit"], payload["size_quote"], payload["risk_quote"],
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def record_live_fill(
    trade_id: int,
    order_id: str,
    fill_price: float,
    fill_ts: str,
    exchange: str,
    order_type: str,
    actual_fee: float,
) -> dict[str, Any]:
    """Record the actual fill details from exchange execution."""
    with _lock:
        conn = _connect()
        conn.execute(
            """
            UPDATE trades SET
                live_order_id = ?,
                live_fill_price = ?,
                live_fill_ts = ?,
                live_exchange = ?,
                live_order_type = ?,
                live_fee_actual = ?
            WHERE id = ?
            """,
            (order_id, fill_price, fill_ts, exchange, order_type, actual_fee, trade_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    return dict(row) if row else None


def close_trade_b(
    trade_id: int,
    exit_price: float,
    status: str,
    r_multiple: float,
    net_r_score: float | None = None,
    actual_slippage: float | None = None,
) -> dict[str, Any] | None:
    """Close a Track B trade with measured execution costs."""
    with _lock:
        conn = _connect()
        trade = get_trade(trade_id)
        if trade is None or trade["track"] != "B":
            return None
        conn.execute(
            """UPDATE trades SET status=?, closed_at=?, exit_price=?,
               r_multiple=?, net_r_score=?, live_slippage_actual=?
               WHERE id=?""",
            (status, _utcnow(), exit_price, round(r_multiple, 4),
             round(net_r_score, 4) if net_r_score is not None else None,
             round(actual_slippage, 8) if actual_slippage is not None else None,
             trade_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    return dict(row) if row else None
```

---

## 3. TRACK B POSITION SIZING

### Fixed fractional, risk-capped

```
Account balance:     $10,000 (example)
Max risk per trade:  1% = $100
Stop distance:       3% (entry → SL)
Position size:       $100 / 0.03 = $3,333

Notional leverage:   0.33× (if spot) or 1× (if futures)
Max concurrent:      2 trades
Max total exposure:  $6,666 (66.7% of account)
```

**Sizing formula (same as Track A but with different risk_pct):**

```python
# config.py — Track B overrides
TRACK_B_MAX_RISK_PCT = float(os.getenv("TRACK_B_MAX_RISK_PCT", "1.0"))
TRACK_B_MAX_CONCURRENT = int(os.getenv("TRACK_B_MAX_CONCURRENT", "2"))
TRACK_B_SYMBOLS = os.getenv("TRACK_B_SYMBOLS", "BTC,ETH").split(",")


def track_b_risk_amount(account_size: float) -> float:
    """1% of account balance, hard-capped."""
    return account_size * TRACK_B_MAX_RISK_PCT / 100.0


def track_b_size_quote(entry: float, stop_loss: float, account_size: float) -> float:
    """Volatility-adjusted position size for Track B.
    
    Same logic as Track A but uses TRACK_B_MAX_RISK_PCT.
    """
    risk_amount = track_b_risk_amount(account_size)
    stop_distance_pct = abs(entry - stop_loss) / entry
    if stop_distance_pct <= 0:
        return 0.0
    size = risk_amount / stop_distance_pct
    # Never exceed 50% of account in a single position
    max_size = account_size * 0.50
    return round(min(size, max_size), 2)
```

**Why 1% and not higher:**
- At 1% risk per trade, max loss on 2 concurrent trades = 2% of account
- This is survivable through a 10-trade losing streak (20% drawdown)
- Enough to measure slippage accurately ($100 risk on $10k = ~$3,300 notional → slippage is measurable at the cent level)

---

## 4. TRACK B METRICS: WHAT TO LOG DIFFERENTLY

### 4a. Entry-side metrics (logged at fill time)

| Field | Source | Why |
|-------|--------|-----|
| `live_fill_price` | Exchange order response | Actual entry, not signal price |
| `live_fill_ts` | Exchange timestamp | Latency measurement |
| `live_order_id` | Exchange | Audit trail |
| `live_order_type` | Limit/Market/IOC | Order quality analysis |
| `live_exchange` | Binance | Multi-exchange later |
| `live_account_id` | Paper/Live identifier | If running multiple accounts |
| `entry_slippage_actual` | `abs(fill_price - signal_entry) / signal_entry` | Real vs modeled entry slippage |

### 4b. Exit-side metrics (logged at close time)

| Field | Source | Why |
|-------|--------|-----|
| `exit_price` | Exchange fill (not candle close) | Actual exit |
| `live_slippage_actual` | `abs(exit_fill - signal_exit) / signal_exit` | Real vs modeled exit slippage |
| `live_fee_actual` | Exchange response | Actual fee (may differ from tier estimate) |
| `latency_entry_ms` | `fill_ts - order_ts` | Execution latency |
| `latency_exit_ms` | Same for exit | Execution latency |

### 4c. Derived metrics (computed, not stored)

```python
def track_b_metrics(trade: dict) -> dict:
    """Compute execution quality metrics for a closed Track B trade."""
    signal_entry = trade["entry"]
    actual_entry = trade["live_fill_price"] or signal_entry
    signal_exit = trade["exit_price"]
    actual_exit = trade.get("live_fill_price") or signal_exit

    entry_slip_bps = (actual_entry - signal_entry) / signal_entry * 10_000
    exit_slip_bps = (actual_exit - signal_exit) / signal_exit * 10_000

    # Paper net R (what Track A would have computed)
    paper_net_r = trade["net_r_score"]

    # Actual net R (using real fills and fees)
    # Re-compute from actual fills
    denom = actual_entry - trade["stop_loss"] if trade["direction"] == "LONG" \
        else trade["stop_loss"] - actual_entry
    move = (actual_exit - actual_entry) if trade["direction"] == "LONG" \
        else (actual_entry - actual_exit)
    fee_penalty = (trade.get("live_fee_actual") or 0) * 2
    actual_net_r = (move - fee_penalty) / abs(denom) if abs(denom) > 1e-15 else 0.0

    execution_drag = paper_net_r - actual_net_r if paper_net_r is not None else None

    return {
        "entry_slippage_bps": round(entry_slip_bps, 2),
        "exit_slippage_bps": round(exit_slip_bps, 2),
        "paper_net_r": paper_net_r,
        "actual_net_r": round(actual_net_r, 4),
        "execution_drag_r": round(execution_drag, 4) if execution_drag is not None else None,
        "execution_drag_pct": round(execution_drag * 100, 2) if execution_drag is not None else None,
        "actual_fee_bps": round(trade.get("live_fee_actual", 0) / trade["size_quote"] * 10_000, 2),
    }
```

---

## 5. PAPER vs LIVE COMPARISON FRAMEWORK

### 5a. The comparison table

```sql
-- Side-by-side: same signal, two executions
SELECT
    t.id,
    t.symbol,
    t.direction,
    t.entry           AS signal_entry,
    t.live_fill_price AS actual_entry,
    t.exit_price      AS signal_exit,
    -- Paper R (Track A style)
    t.r_multiple      AS paper_r,
    t.net_r_score     AS paper_net_r,
    -- Actual R (Track B with real fills)
    t.live_slippage_actual AS actual_slippage,
    t.live_fee_actual      AS actual_fee,
    -- Execution drag
    (t.net_r_score - (
        (t.exit_price - t.entry) / abs(t.entry - t.stop_loss)
        - t.live_fee_actual * 2 / abs(t.entry - t.stop_loss)
    )) AS execution_drag_r
FROM trades t
WHERE t.track = 'B'
  AND t.status IN ('SL_HIT', 'TP_HIT', 'MANUAL_CLOSE')
ORDER BY t.closed_at DESC;
```

### 5b. Key comparison metrics

| Metric | Formula | What it tells you |
|--------|---------|-------------------|
| **Entry slippage** | `(actual_entry - signal_entry) / signal_entry` | How much worse than signal price you actually entered |
| **Exit slippage** | `(actual_exit - signal_exit) / signal_exit` | How much worse than signal price you actually exited |
| **Fee accuracy** | `actual_fee / estimated_fee` | Whether your fee tier assumptions are right |
| **Execution drag** | `paper_net_r - actual_net_r` | Total cost of real execution vs paper |
| **Drag consistency** | `stddev(drag)` across trades | Whether slippage is predictable or random |
| **Regime drag** | Group drag by `atr_percentile` buckets | Whether slippage model needs regime adjustment |

### 5c. Calibration target

After n≥20 Track B trades, compute:

```sql
SELECT
    -- Is our slippage model biased?
    AVG(live_slippage_actual - entry_slippage) AS mean_slippage_bias,
    
    -- Is our fee model accurate?
    AVG(live_fee_actual / (entry * 0.001)) AS fee_accuracy_ratio,
    
    -- Total execution drag in R terms
    AVG(net_r_score - actual_net_r) AS mean_drag_r,
    
    -- Does drag increase in volatile regimes?
    CASE 
        WHEN (SELECT AVG(atr_pct) FROM trade_regime WHERE id = t.id) > 0.7
        THEN 'high_vol'
        ELSE 'low_vol'
    END AS regime,
    AVG(live_slippage_actual) AS avg_slip_by_regime
FROM trades t
WHERE track = 'B' AND status != 'OPEN';
```

---

## 6. GO-LIVE CHECKLIST

### Pre-flight (do once, before first Track B trade)

- [ ] **API keys created** — Binance testnet first, then mainnet
  - API key with: Spot trading enabled, Futures trading enabled
  - IP whitelist: your server IP only
  - **No withdrawal permissions** — ever
- [ ] **Testnet dry run** — execute 5 trades on Binance testnet
  - Verify order placement, fill, cancellation workflow
  - Confirm API response parsing (order ID, fill price, fees)
  - Time the round-trip: signal → order → fill → log
- [ ] **Kill switch implemented** — see below
- [ ] **Rate limiting** — max 10 orders/second (Binance limit), implement exponential backoff
- [ ] **Error handling** — network timeout → no order placed (never retry market orders blindly)
- [ ] **Position size validation** — `size_quote ≤ account_balance * 0.50`
- [ ] **Order type decided** — **LIMIT IOC recommended** (guaranteed fill or cancel, no resting orders)
- [ ] **Monitoring alert** — Telegram/Slack notification on every fill and every error
- [ ] **Backup plan** — manual exchange login credentials accessible if bot crashes

### Kill switch design

```python
# config.py
KILL_SWITCH_ENABLED = os.getenv("KILL_SWITCH_ENABLED", "true").lower() == "true"
KILL_SWITCH_FILE = Path(os.getenv("KILL_SWITCH_FILE", "KILL_SWITCH"))


def kill_switch_active() -> bool:
    """If KILL_SWITCH file exists, no live orders are placed."""
    if not KILL_SWITCH_ENABLED:
        return False
    return KILL_SWITCH_FILE.exists()
```

```bash
# Emergency stop — creates a file that blocks all live orders
touch KILL_SWITCH

# Resume — removes the file
rm KILL_SWITCH
```

```python
# In the live execution path (service.py or new live_executor.py)
from .config import kill_switch_active

def execute_track_b(signal, account_size):
    if kill_switch_active():
        logger.critical("KILL SWITCH ACTIVE — no live orders placed")
        return None
    # ... proceed with order placement
```

### Operational monitoring

| Metric | Alert threshold | Action |
|--------|----------------|--------|
| `kill_switch_active()` | Any time | Halt all live orders, notify |
| `track_b_drawdown > 5%` | Daily check | Pause Track B, review |
| `actual_slippage > 3× modeled` | Per trade | Log, investigate |
| `API error rate > 5%` | Hourly | Pause Track B, check exchange status |
| `open order age > 60s` | Real-time | Cancel IOC order, log anomaly |
| `Balance < $500` | Daily | Pause Track B, notify |

---

## 7. DOCUMENTATION TEMPLATE FOR n≥50 DECISION

### Track A alpha report (unchanged from existing scorecard)

```sql
-- Primary alpha sample — what matters for strategy validation
SELECT
    COUNT(*) AS n_trades,
    SUM(CASE WHEN r_multiple > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS win_rate_pct,
    AVG(r_multiple) AS expectancy_r,
    SUM(CASE WHEN r_multiple > 0 THEN r_multiple ELSE 0 END) /
        ABS(SUM(CASE WHEN r_multiple <= 0 THEN r_multiple ELSE 0 END)) AS profit_factor,
    MAX(net_r_score) AS best_trade,
    MIN(net_r_score) AS worst_trade
FROM trades
WHERE track = 'A' AND status != 'OPEN';
```

### Track B execution report

```sql
-- Execution quality — what matters for cost calibration
SELECT
    COUNT(*) AS n_trades,
    
    -- Entry quality
    AVG(live_slippage_actual) AS avg_entry_slippage,
    STDDEV(live_slippage_actual) AS std_entry_slippage,
    
    -- Fee accuracy
    AVG(live_fee_actual / (entry * 0.001)) AS fee_accuracy_ratio,
    
    -- Execution drag
    AVG(net_r_score - (
        (exit_price - entry) / ABS(entry - stop_loss)
        - live_fee_actual * 2 / ABS(entry - stop_loss)
    )) AS mean_drag_r,
    
    -- Regime breakdown
    AVG(CASE WHEN direction = 'LONG' THEN live_slippage_actual END) AS long_slip,
    AVG(CASE WHEN direction = 'SHORT' THEN live_slippage_actual END) AS short_slip,
    
    -- Worst case
    MAX(live_slippage_actual) AS worst_entry_slippage,
    MAX(live_fee_actual) AS worst_fee
FROM trades
WHERE track = 'B' AND status != 'OPEN';
```

### Decision matrix

| Outcome | Track A (n≥50) | Track B (n≥20) | Decision |
|---------|---------------|----------------|----------|
| **1. Full live** | Expectancy > 0.3R, PF > 1.5 | Mean drag < 0.1R, slippage < 2× modeled | Proceed to Phase E (live order routing) |
| **2. Adjust costs only** | Expectancy > 0.3R | Mean drag > 0.1R but predictable | Update slippage model, keep paper-only until recalibrated |
| **3. Strategy weak** | Expectancy < 0.3R | N/A (irrelevant) | Do not go live; strategy needs work |
| **4. Mixed** | Expectancy 0.15-0.3R | Drag > 0.2R | Extend paper sample to n≥100; investigate |
| **5. Fail** | Any | Any | Stop. Diagnose. Re-paper from scratch |

### Critical numbers to track

```python
# Weekly log entry — append to a markdown file
WEEKLY_LOG = """
## Week of {date}

### Track A (Paper)
- Closed trades this week: {a_week_count}
- Cumulative R: {a_cum_r:.2f}
- Active positions: {a_active}

### Track B (Micro-live)
- Trades executed this week: {b_week_count}
- Mean entry slippage: {b_mean_slip:.2f} bps
- Mean execution drag: {b_mean_drag:.4f} R
- Max single-trade drag: {b_max_drag:.4f} R
- Kill switch triggered: {b_kills}

### Action items
{action_items}
"""
```

---

## 8. WHAT CHANGES vs WHAT STAYS FROZEN

| Component | Status | Reason |
|-----------|--------|--------|
| `signals.py` (gates, thresholds, multipliers) | **FROZEN** | Alpha estimation must not be contaminated |
| `indicators.py` | **FROZEN** | Same reason |
| `patterns.py` | **FROZEN** | Same reason |
| `risk.py` | **FROZEN** | Same reason |
| `config.py` | **ADDITIVE** only | New env vars for Track B sizing, kill switch |
| `store.py` | **ADDITIVE** only | New columns, new functions, schema migration |
| `service.py` | **ADDITIVE** only | New `open_trades_b()`, `close_trade_b()` functions |
| `derivatives.py` (new) | **NEW** | Confidence modifier only, no entry triggers |
| `slippage.py` (new) | **NEW** | Models cost assumptions, doesn't change strategy |
| `live_executor.py` (new) | **NEW** | Binance order placement, isolated from signal logic |

**The wall is structural, not just procedural.** Track A trades are never touched by live execution code. Track B trades are never included in the alpha scorecard. The signal engine produces one output; two consumers read it independently.

---

## SUMMARY

| Deliverable | What it is | Why it matters |
|-------------|-----------|---------------|
| `track` column on trades | Separates A from B in the DB | Clean queries, no data mixing |
| `live_*` fields | Actual fill prices, timestamps, fees | Measures real execution quality |
| Track B sizing (1% risk, 2 max) | Conservative, survivable | Protects capital during calibration |
| Kill switch | File-based, instant | Emergency stop without code changes |
| Comparison metrics | Execution drag, slippage bias, fee accuracy | Calibrates the slippage model from Phase B |
| Decision matrix | n≥50 + n≥20 thresholds | Clear go/no-go criteria |

The entire design is additive — zero changes to signal logic, zero changes to Track A. Want me to implement any of these components?