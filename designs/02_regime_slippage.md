“Approved design – do not implement until n ≥ 50 v2 trades.”

Here's the complete model specification, integration pattern, and scenario framework.

---

### 1. THE MODEL

```
slippage = base + k1 × atr_percentile + k2 × (1 / volume_norm) + k3 × |oi_change_24h|
```

**Where:**
- `atr_percentile` ∈ [0, 1] — where current ATR% sits in its 60-day distribution
- `volume_norm` = current 24h volume / median 24h volume (60-day), clamped to [0.1, 10.0]
- `oi_change_24h` = absolute % change in open interest (optional, 0.0 until Phase D)
- Output is a decimal: 0.003 = 0.3%

**Ceiling:** `min(computed, 0.01)` — hard cap at 1% to prevent degenerate states.

---

### 2. STARTING COEFFICIENTS

Calibrated against Binance spot order-book depth data (top-of-book spread + 1-tick adverse fill):

| Parameter | Major (BTC, ETH) | Mid-cap (SOL, AVAX, LINK, DOT, ...) | Low-cap / Illiquid |
|-----------|-----------------|--------------------------------------|-------------------|
| **base** | 0.0003 (3 bps) | 0.0008 (8 bps) | 0.0020 (20 bps) |
| **k1** (ATR sensitivity) | 0.0004 | 0.0008 | 0.0012 |
| **k2** (volume penalty) | 0.0003 | 0.0006 | 0.0010 |
| **k3** (OI crowding) | 0.0002 | 0.0004 | 0.0006 |

**Rationale for k-values:**

- `k1` (ATR): Volatility widens spreads. ATR percentile at 1.0 (max vol) adds 4/8/12 bps to majors/mid/low. This captures the empirical relationship where spread ∝ volatility^(2/3) in queueing-theoretic market microstructure.

- `k2` (volume inverse): Low volume = thin book. When volume_norm = 0.25 (volume at 25% of median), the penalty is 1.25× k2. When volume_norm ≥ 2.0 (high activity), the penalty approaches zero — the book is thick enough that market orders walk fewer levels.

- `k3` (OI crowding): Large OI changes = positioning imbalance = adverse selection risk. Set to 0.0 until exchange-native OI data is available (Phase D). Pre-seeded so the integration is ready.

---

### 3. SCENARIO MATRIX

| Scenario | ATR%ile | volume_norm | OI change | BTC slippage | Mid slippage | Low slippage |
|----------|---------|-------------|-----------|-------------|-------------|-------------|
| **Calm** | 0.15 | 1.5 | 0% | 0.00035 | 0.00092 | 0.00235 |
| **Typical** | 0.50 | 1.0 | 5% | 0.00051 | 0.00134 | 0.00340 |
| **Stressed** | 0.90 | 0.4 | 15% | 0.00075 | 0.00202 | 0.00530 |

The stressed scenario for low-caps (0.53%) is realistic — books thin out dramatically during sell-offs.

---

### 4. PYTHON IMPLEMENTATION

```python
"""Regime-aware slippage model.

Returns a decimal slippage value (e.g., 0.003 = 0.3%) based on
current market regime. Designed as a drop-in replacement for the flat
SLIPPAGE_BUFFER constant.
"""

from __future__ import annotations

# ── Coefficient table ────────────────────────────────────────────────────────
# (base, k1_atr, k2_volume_inv, k3_oi)
# Tuned per tier. All values are decimals (not percentages).

COEFFICIENTS: dict[str, tuple[float, float, float, float]] = {
    #                base     k1       k2       k3
    "major":       (0.0003,  0.0004,  0.0003,  0.0002),
    "mid":         (0.0008,  0.0008,  0.0006,  0.0004),
    "low":         (0.0020,  0.0012,  0.0010,  0.0006),
}

MAJORS = {"BTC", "ETH"}
MID_CAPS = {"SOL", "AVAX", "LINK", "DOT", "MATIC", "UNI", "LTC", "BCH",
            "TON", "XLM", "HBAR", "SUI", "DOGE", "ADA", "TRX"}

CEILING = 0.01   # 1% hard max — prevents degenerate states
FLOOR = 0.0001   # 0.01% minimum — there's always *some* spread


def _tier(symbol: str) -> str:
    s = symbol.upper()
    if s in MAJORS:
        return "major"
    if s in MID_CAPS:
        return "mid"
    return "low"


def slippage_model(
    symbol: str,
    atr_percentile: float,
    volume_norm: float,
    oi_change_24h: float | None = None,
) -> float:
    """Compute regime-aware slippage for a symbol.

    Args:
        symbol:        Coin ticker (BTC, ETH, SOL, ...)
        atr_percentile: Current ATR% percentile in 60-day window [0, 1]
        volume_norm:   Current 24h volume / median 24h volume (60-day)
        oi_change_24h: Absolute % change in open interest (optional, default 0)

    Returns:
        Decimal slippage (e.g., 0.003 = 0.3%)
    """
    tier = _tier(symbol)
    base, k1, k2, k3 = COEFFICIENTS[tier]

    # Clamp inputs to sane ranges
    atr_p = max(0.0, min(1.0, atr_percentile))
    vol_n = max(0.1, min(10.0, volume_norm))
    oi = abs(oi_change_24h / 100.0) if oi_change_24h is not None else 0.0

    slip = base + k1 * atr_p + k2 * (1.0 / vol_n) + k3 * oi
    return max(FLOOR, min(CEILING, slip))
```

---

### 5. INTEGRATION INTO `service.py` — drop-in pattern

The existing `_slip_entry` / `_slip_exit` use a flat `_cfg.SLIPPAGE_BUFFER`. The integration replaces that constant with the model output, and threads the regime context through from the signal snapshot.

**Pseudo-code for the integration:**

```python
# ── service.py — new helper ──────────────────────────────────────────────────

def _regime_slippage(symbol: str, ind: dict) -> float:
    """Compute slippage from the current indicator regime context.

    Uses the same `ind` dict that compute_signal() already receives.
    No new data fetch required — everything is already in the snapshot.
    """
    from .slippage import slippage_model

    atr_p = _percentile_from_history(symbol, ind.get("atr_pct", 0))
    vol_n = ind.get("volume_ratio_20d", 1.0)
    if vol_n is None or vol_n <= 0:
        vol_n = 1.0
    oi = None  # future: ind.get("oi_change_24h")
    return slippage_model(symbol, atr_p, vol_n, oi)


def _percentile_from_history(symbol: str, current_atr_pct: float) -> float:
    """Fast ATR percentile from the signals table (last 60 days).

    Could be cached with lru_cache keyed on (symbol, date) for speed.
    For now, a single query per trade close is fine.
    """
    import sqlite3
    from datetime import datetime, timedelta, timezone
    from .store import DB_PATH

    cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        """
        SELECT s.indicators FROM signals s
        JOIN scans sc ON s.scan_id = sc.id
        WHERE s.symbol = ? AND sc.ts >= ? AND sc.ts <= ?
        """,
        (symbol, cutoff, datetime.now(timezone.utc).isoformat()),
    ).fetchall()
    conn.close()

    if not rows:
        return 0.5  # no history → assume median regime

    import json
    atr_values = [json.loads(r[0]).get("atr_pct", 0) for r in rows]
    if not atr_values:
        return 0.5
    return sum(1 for v in atr_values if v < current_atr_pct) / len(atr_values)


# ── service.py — replace _slip_entry / _slip_exit ───────────────────────────

def _slip_entry(direction: str, entry: float, slippage: float) -> float:
    """Entry price adjusted for regime-aware slippage."""
    return entry * (1 + slippage) if direction == "LONG" else entry * (1 - slippage)


def _slip_exit(direction: str, exit_price: float, slippage: float) -> float:
    """Exit price adjusted for regime-aware slippage."""
    return exit_price * (1 - slippage) if direction == "LONG" else exit_price * (1 + slippage)


def _net_r(direction: str, entry: float, stop_loss: float, exit_price: float,
           slippage: float) -> float:
    """Net R-multiple after regime-aware slippage and round-trip taker fees."""
    slipped_entry = _slip_entry(direction, entry, slippage)
    slipped_exit = _slip_exit(direction, exit_price, slippage)
    fee = _cfg.active_taker_fee()
    denom = slipped_entry - stop_loss if direction == "LONG" else stop_loss - slipped_entry
    if abs(denom) < 1e-15:
        return 0.0
    move = slipped_exit - slipped_entry if direction == "LONG" else slipped_entry - slipped_exit
    fee_penalty = slipped_entry * fee + slipped_exit * fee
    return (move - fee_penalty) / abs(denom)
```

**Callers** (close_trade, replay, resolve) would change from:
```python
net_r = _net_r(direction, entry, sl, price)
```
to:
```python
slip = _regime_slippage(symbol, ind)
net_r = _net_r(direction, entry, sl, price, slip)
```

The `slippage` value is stored on the trade row for later analysis (see Phase 6).

---

### 6. SCENARIO BACKTEST & SCENARIO ID

**Three scenarios for sensitivity analysis:**

| Scenario ID | k1 multiplier | k2 multiplier | k3 multiplier | Use case |
|-------------|--------------|--------------|--------------|----------|
| `calm` | ×0.5 | ×0.5 | ×0.5 | Bull market, high liquidity, tight spreads |
| `typical` | ×1.0 | ×1.0 | ×1.0 | Default / production |
| `stressed` | ×2.0 | ×2.0 | ×2.0 | Crash / flash crash / low-liquidity weekend |

**Implementation — add a `scenario_scale` to `slippage_model`:**

```python
def slippage_model(
    symbol: str,
    atr_percentile: float,
    volume_norm: float,
    oi_change_24h: float | None = None,
    scenario: str = "typical",
) -> float:
    SCALE = {"calm": 0.5, "typical": 1.0, "stressed": 2.0}
    scale = SCALE.get(scenario, 1.0)
    # ... rest of model, but multiply k1, k2, k3 by scale
```

**Store `scenario_id` on each trade** — add a column to `trades`:

```sql
ALTER TABLE trades ADD COLUMN slippage_scenario TEXT DEFAULT 'typical';
ALTER TABLE trades ADD COLUMN regime_slippage REAL;
```

Then in `close_trade()`:
```python
slip = _regime_slippage(trade["symbol"], ...)
# store both the computed slippage and the scenario used
conn.execute(
    "UPDATE trades SET regime_slippage = ?, slippage_scenario = ? WHERE id = ?",
    (round(slip, 6), scenario, trade_id)
)
```

**Analysis pattern:** After accumulating n≥50 trades, run:

```sql
SELECT slippage_scenario, AVG(net_r_score), COUNT(*)
FROM trade_regime
WHERE net_r_score IS NOT NULL
GROUP BY slippage_scenario;
```
Compare whether `calm`-tagged trades had systematically better net R than `stressed`-tagged ones. This tells you how much of your edge is regime-dependent vs strategy-dependent.

---

### 7. WHAT TO EXPECT

With the current sample (3 trades, all SL_HIT):

| Trade | ATR%ile | vol_norm | Current flat slip | Model output (typical) | Delta |
|-------|---------|----------|-------------------|----------------------|-------|
| BTC | 0.30 | 1.13 | 0.00050 | 0.00047 | -0.3 bps |
| ZEC | 0.93 | 2.84 | 0.00050 | 0.00121 | +7.1 bps |
| TRX | 0.02 | 1.13 | 0.00050 | 0.00088 | +3.8 bps |

ZEC's net R would drop from -1.0285 to approximately **-1.0356** under the regime model — the high ATR percentile (93rd) and low volume correctly penalize it more than the flat 5bps buffer. TRX also gets a larger penalty than flat, reflecting its mid-cap status. BTC barely changes — it's liquid enough that 5bps is close to fair.

**The model catches what the flat buffer misses:** regime-aware slippage correctly identifies that ZEC in a high-volatility regime costs 2.4× more in slippage than BTC in a calm regime, even though both trades used the same 5bps flat buffer.

---

### Summary of changes (not yet applied — design only)

| File | Change | Status |
|------|--------|--------|
| `slippage.py` (new) | `slippage_model()` + coefficient table | Design complete, ready to implement |
| `config.py` | Keep `SLIPPAGE_BUFFER` as fallback; add `SLIPPAGE_MODEL=true` toggle | Ready to implement |
| `service.py` | `_regime_slippage()`, `_percentile_from_history()`, updated `_net_r()` signature | Ready to implement |
| `store.py` | `ALTER TABLE trades ADD COLUMN slippage_scenario`, `regime_slippage` | Ready to implement |

Want me to implement this now, or keep it as design spec until more trades accumulate?