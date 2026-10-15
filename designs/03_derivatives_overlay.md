“Approved design – do not implement until n ≥ 50 v2 trades.”

## Objective
- Build a complete crypto signal engine with dashboard, paper-trade journal, dual-engine (v1/v2) scoring, and hybrid execution modes (SPOT/FUTURES) for forward-testing before live capital deployment
- Currently building regime analysis tool to correlate trade outcomes with market regime factors

## Important Details
- User is "Shahmeeer" building a self-hosted crypto trading signal system; funds stay under human control until validated
- Architecture: CoinGecko (universe) → Binance public API (OHLCV, replaces yfinance) → indicators → signals → dashboard/journal
- Forward-test freeze rule: no alpha logic changes during n≥50 sample collection; old trades = v1 sample, new = v2
- Phase A (LOCKED): signal engine + paper journal + dashboard UI
- Phase B (COMPLETED): Hybrid Toggle (SPOT/FUTURES), regime guard, Net-R scoring with slippage + fees, Binance API migration, DB deadlock fix
- Phase C (PENDING): Exchange-native data, fee tiers, order-book depth
- Phase D (PENDING): Funding rate, OI, correlation-aware sizing, trailing stops
- Phase E (PENDING): Live order routing, monitoring, walk-forward optimization
- Engine version tracking: `engine_version_at_compute` key in all outputs
- C: drive critically full (0.05 GB); uv cache redirected to `D:\All\Downloads\.uvcache`
- Package installed editable via `.pth` (code edits live without reinstall)
- SQLite DB at `dashboard.db` project root; schema uses `scans` table with autoincrement IDs
- Binance public API (`/api/v3/klines`) replaced yfinance as primary OHLCV source — 0.3s/coin vs 10-30s
- yfinance fallback removed from scan path entirely to prevent blocking; coins not on Binance are skipped
- `threading.Lock` changed to `threading.RLock` in store.py to fix deadlock in `close_trade()` → `get_trade()` re-entrant call
- Startup replay timeout capped at 30s in app.py lifespan
- Scan budget: 2-minute hard cap, 20s per-coin timeout as safety nets

## Work State
### Completed
- Full project audit: identified 9 weaknesses, verified architecture
- `store.py`: SQLite layer — scans, signals (scan_id-keyed), trades (18 columns incl. net_r_score/entry_slippage/exit_slippage/fee_paid), settings; RLock; schema migration for pre-existing DBs
- `service.py`: scan worker (2-min budget, Binance OHLCV, over-fetch 2×, stablecoin filter, degenerate-data guard, ATR=0 filter), paper-trade lifecycle, candle-replay reconciliation, scorecard with go/no-go bars, `_net_r()` / `_r_multiple()` / `_slip_entry()` / `_slip_exit()`, `close_trade()` with yfinance fallback removed
- `app.py`: FastAPI backend — 8 JSON endpoints, background worker, lifespan with 30s replay timeout + asyncio.wait_for, port-guard
- `static/index.html`: Full dark-theme dashboard UI — signals table, position cards, scorecard chips, equity curve, chart modal (lightweight-charts v4 CDN)
- `config.py`: execution_mode() (SPOT/FUTURES), SPOT_TAKER_FEE=0.0010, FUTURES_TAKER_FEE=0.0004, SLIPPAGE_BUFFER=0.0005, active_taker_fee()
- `indicators.py`: v2 engine — StochRSI(14,14,3,3), directional_pressure(), gradient_trend() (tanh-normalized in ATR units); all v1 keys preserved
- `patterns.py`: strict engulfing, pinbar (tail≥2.5×body), liquidity sweep, `strengths(df)` (per-side bullish/bearish_strength [0,1]), `detect()`, `latest()` with pat_bullish_strength, pat_bearish_strength, pat_max_strength
- `risk.py`: RiskPlan dataclass, fractal pivots, structural stops, vol-stretched TP
- `signals.py`: Dual-engine dispatch — v1 legacy (FactorScores, weighted factors), v2 layered matrix (TREND_GATE_MIN=0.35, PATTERN_STRENGTH_MIN=0.75, PATTERN_CONF_MULTIPLIER=1.25, StochRSI trigger, risk.py injection); `_apply_regime_guard()` suppresses SHORT in SPOT mode; `signal_to_dict()` for both types
- `market_data.py`: Binance primary (`/api/v3/klines`, 10s timeout), CoinGecko top coins, yfinance removed from main path; `_INTERVAL_MAP`, `_PERIOD_LIMIT`, SYMBOL_TO_YF mapping kept for fallback reference
- `pyproject.toml`: deps include fastapi, uvicorn, httpx, python-dotenv; scripts: crypto-dashboard, crypto-signals, crypto-mcp
- README: architecture diagram (dual-engine), V2 signal matrix section, config env vars, Net-R formula, forward-test rule
- DB deadlock fix: `threading.Lock` → `threading.RLock` in store.py
- Binance API migration: full scan now completes in ~7.7s (was 5+ min with yfinance)
- Phase B verification: v1 regression, v2 gates, risk injection, pattern multiplier, regime guard, Net-R scoring all tested and passing
- Live data: 5 open trades (TRX, BNB, ZEC, ETH, LEO); 3 closed (BTC SHORT SL_HIT r=-1.00 net_r=None pre-Phase-B, ZEC LONG SL_HIT r=-1.00 net_r=-1.0285, TRX LONG SL_HIT r=-1.00 net_r=-1.1365)

### Active
- **Building `regime_analysis.py`**: Script written at `D:\All\Downloads\crypto\regime_analysis.py` — loads closed trades, joins to signal snapshots, computes regime tags (grad_at_entry, atr_percentile, volume_ratio_20d, pat_side_strength), saves `trade_regime` table, runs OLS regression + correlation analysis on net_r_score, outputs `regime_report.md` + `trade_regime.csv`. Not yet run.
- Known data limitation: 3 closed trades, all SL_HIT with r=-1.00 (zero gross R variance); many v2 fields NULL for trades opened during v1 scans (scans 1-24 lack gradient_trend, pat_*_strength, stochrsi, net_volume_flow); v2 fields start from scan #25

### Blocked
- regime_analysis.py has not been executed yet — needs `uv run python regime_analysis.py` to run; may need statsmodels installed

## Next Move
1. Run `uv run python regime_analysis.py` — check if statsmodels is available or needs installing; if missing, add to pyproject.toml and re-sync
2. Review generated `regime_report.md` and `trade_regime.csv` for findings
3. Continue accumulating v2 trades toward n≥50 forward-test threshold

## Relevant Files
- `D:\All\Downloads\crypto\src\crypto\signals.py` — dual-engine dispatch + regime guard
- `D:\All\Downloads\crypto\src\crypto\config.py` — execution mode, fees, slippage
- `D:\All\Downloads\crypto\src\crypto\market_data.py` — Binance primary OHLCV
- `D:\All\Downloads\crypto\src\crypto\patterns.py` — engulfing/pinbar/sweep + per-side strengths
- `D:\All\Downloads\crypto\src\crypto\indicators.py` — v2 TA (StochRSI, gradient_trend, directional_pressure)
- `D:\All\Downloads\crypto\src\crypto\risk.py` — structural stops, vol-stretched TP
- `D:\All\Downloads\crypto\src\crypto\service.py` — scan worker, paper-trade lifecycle, Net-R, close_trade with yfinance fallback
- `D:\All\Downloads\crypto\src\crypto\store.py` — SQLite (RLock, 18-col trades, schema migration)
- `D:\All\Downloads\crypto\src\crypto\app.py` — FastAPI (30s replay timeout, broad exception catch on DELETE)
- `D:\All\Downloads\crypto\src\crypto\static\index.html` — dashboard UI
- `D:\All\Downloads\crypto\regime_analysis.py` — regime tagging + regression analysis (written, not yet run)
- `D:\All\Downloads\crypto\dashboard.db` — SQLite DB (29 scans, 3 closed trades, 5 open trades)
- `D:\All\Downloads\crypto\README.md` — updated with v2 architecture, config, Net-R
- `D:\All\Downloads\crypto\pyproject.toml` — deps and scripts
- `D:\All\Downloads\.uvcache` — redirected uv cache (C: drive critically low)

Coverage is 89% — almost all scan coins have Binance perpetual futures. Here's the complete design:

---

## 1. MODULE: `derivatives.py`

```python
"""Derivatives overlay — OI + Funding Rate signals.

Lightweight module fetching public Binance futures data (no API key required).
Designed as a confidence modifier / go-no-go gate, NOT as an entry trigger.

Endpoints used (all public, rate-limited 1200 req/min):
  /fapi/v1/premiumIndex      → current funding rate (1 req)
  /fapi/v1/fundingRate        → 30d funding history (1 req, limit=240)
  /fapi/v1/openInterest       → current OI (1 req)
  /fapi/v1/futures/data/openInterestHist → OI history (1 req, limit=25 for 24h delta)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import httpx

logger = logging.getLogger("crypto.derivatives")

_BASE = "https://fapi.binance.com"
_TIMEOUT = 8.0

# ── Mapping: scan symbol → Binance perpetual pair ────────────────────────────
# Some scan coins use a "1000×" prefix on Binance (PEPE→1000PEPEUSDT).
# If a coin has no perpetual, the fetch returns None gracefully.

_SCAN_TO_FUTURES: dict[str, str] = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "BNB": "BNBUSDT", "SOL": "SOLUSDT",
    "XRP": "XRPUSDT", "DOGE": "DOGEUSDT", "ADA": "ADAUSDT", "AVAX": "AVAXUSDT",
    "DOT": "DOTUSDT", "LINK": "LINKUSDT", "TRX": "TRXUSDT", "UNI": "UNIUSDT",
    "LTC": "LTCUSDT", "BCH": "BCHUSDT", "TON": "TONUSDT", "XLM": "XLMUSDT",
    "HBAR": "HBARUSDT", "SUI": "SUIUSDT", "NEAR": "NEARUSDT", "APT": "APTUSDT",
    "FIL": "FILUSDT", "ATOM": "ATOMUSDT", "INJ": "INJUSDT", "IMX": "IMXUSDT",
    "AAVE": "AAVEUSDT", "ETC": "ETCUSDT", "ARB": "ARBUSDT", "OP": "OPUSDT",
    "MKR": "MKRUSDT", "CRV": "CRVUSDT", "GRT": "GRTUSDT", "RENDER": "RENDERUSDT",
    "STX": "STXUSDT", "SEI": "SEIUSDT", "FET": "FETUSDT", "WLD": "WLDUSDT",
    "TIA": "TIAUSDT", "JUP": "JUPUSDT", "ONDO": "ONDOUSDT", "ENS": "ENSUSDT",
    "DYDX": "DYDXUSDT", "PENDLE": "PENDLEUSDT",
    "PEPE": "1000PEPEUSDT", "SHIB": "1000SHIBUSDT",
    "BONK": "1000BONKUSDT", "WIF": "1000WIFUSDT", "FLOKI": "1000FLOKIUSDT",
}

# Coins with no Binance perpetual at all
_NO_FUTURES = {"LEO", "OKB", "MATIC"}


# ── Data containers ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DerivSnapshot:
    """Immutable snapshot of derivatives state at one point in time."""
    symbol: str
    oi_usd: float | None = None
    oi_change_1h: float | None = None      # fractional, e.g. 0.02 = +2%
    oi_change_24h: float | None = None
    funding_rate: float | None = None       # per-8h rate, e.g. 0.0001 = 1 bps
    funding_8h_avg: float | None = None     # mean of last 3 funding payments
    funding_percentile_30d: float | None = None  # [0, 1]


# ── Fetchers ─────────────────────────────────────────────────────────────────

def _pair(symbol: str) -> str | None:
    """Resolve scan symbol to Binance futures pair, or None if unavailable."""
    s = symbol.upper().replace("1000", "")  # normalize
    if s in _NO_FUTURES:
        return None
    return _SCAN_TO_FUTURES.get(s)


def fetch_oi_funding(symbol: str) -> DerivSnapshot | None:
    """Fetch OI + funding for a coin. Returns None if no perpetual exists.

    Makes at most 4 HTTP requests (all parallel-safe, sequential for simplicity).
    Rate budget: ~4 requests per coin per scan. With 50 coins = 200 req/scan.
    At 1200 req/min, this is ~10 seconds worst case (typical: <2s with keepalive).
    """
    pair = _pair(symbol)
    if pair is None:
        return None

    client = httpx.Client(timeout=_TIMEOUT, headers={"Accept": "application/json"})
    try:
        # --- Funding rate (current + history) ---
        prem = client.get(f"{_BASE}/fapi/v1/premiumIndex", params={"symbol": pair})
        if prem.status_code != 200:
            logger.warning("premiumIndex %s returned %d", pair, prem.status_code)
            return None
        pd_ = prem.json()
        funding_rate = float(pd_.get("lastFundingRate", 0))

        hist = client.get(
            f"{_BASE}/fapi/v1/fundingRate",
            params={"symbol": pair, "limit": 240},  # 30 days × 3/day
        )
        rates = [float(x["fundingRate"]) for x in hist.json()] if hist.status_code == 200 else []

        # Last 3 payments ≈ 24h average
        avg_8h = sum(rates[-3:]) / min(3, len(rates)) if rates else funding_rate
        # Percentile across full 30d history
        pctile = _percentile(rates, funding_rate) if len(rates) >= 10 else 0.5

        # --- Open Interest (current + 24h change) ---
        oi_resp = client.get(f"{_BASE}/fapi/v1/openInterest", params={"symbol": pair})
        if oi_resp.status_code != 200:
            logger.warning("openInterest %s returned %d", pair, oi_resp.status_code)
            return None
        oi_now = float(oi_resp.json().get("openInterest", 0)) * float(pd_.get("markPrice", 1))

        hist_oi = client.get(
            f"{_BASE}/futures/data/openInterestHist",
            params={"symbol": pair, "period": "1h", "limit": 25},
        )
        oi_change_24h = None
        oi_change_1h = None
        if hist_oi.status_code == 200 and len(hist_oi.json()) >= 2:
            oh = hist_oi.json()
            oi_vals = [float(x["sumOpenInterestValue"]) for x in oh]
            if len(oi_vals) >= 2 and oi_vals[0] > 0:
                oi_change_24h = (oi_vals[-1] - oi_vals[0]) / oi_vals[0]
            if len(oi_vals) >= 2 and oi_vals[-2] > 0:
                oi_change_1h = (oi_vals[-1] - oi_vals[-2]) / oi_vals[-2]

        return DerivSnapshot(
            symbol=symbol.upper(),
            oi_usd=oi_now,
            oi_change_1h=oi_change_1h,
            oi_change_24h=oi_change_24h,
            funding_rate=funding_rate,
            funding_8h_avg=avg_8h,
            funding_percentile_30d=pctile,
        )
    except Exception as exc:
        logger.debug("fetch_oi_funding(%s) failed: %s", symbol, exc)
        return None
    finally:
        client.close()


def _percentile(history: list[float], current: float) -> float:
    """Where does `current` sit in `history`? Returns [0, 1]."""
    if not history:
        return 0.5
    below = sum(1 for v in history if v <= current)
    return below / len(history)


# ── Scoring functions ────────────────────────────────────────────────────────

def compute_positioning_score(
    oi_change_24h: float | None,
    funding_percentile: float | None,
) -> float:
    """Crowding / positioning score in [-1, 1].

    Positive = bullish crowding (longs paying shorts, OI expanding).
    Negative = bearish crowding (shorts paying longs, OI expanding).

    Logic:
      funding_pctl > 0.5 → longs are paying shorts → bullish crowding
      funding_pctl < 0.5 → shorts are paying longs → bearish crowding
      OI expansion amplifies the signal (more conviction on the crowded side)
      OI contraction dampens it (positions closing = less conviction)

    Returns 0.0 if either input is None.
    """
    if oi_change_24h is None or funding_percentile is None:
        return 0.0

    # Funding signal: how far from neutral (0.5 = neutral funding)
    # Maps [0, 1] → [-1, 1] with 0.5 as center
    fund_signal = (funding_percentile - 0.5) * 2.0

    # OI momentum: expansion (+) or contraction (-)
    # Saturates at ±20% change to avoid outlier distortion
    oi_momentum = max(-1.0, min(1.0, oi_change_24h / 0.20))

    # Combine: funding determines direction, OI determines conviction
    # When OI contracts while funding is extreme, it means capitulation → weaker signal
    conviction = 1.0 + 0.5 * oi_momentum  # ranges [0.5, 1.5]

    return max(-1.0, min(1.0, fund_signal * conviction))


def compute_oi_velocity(
    price_change_24h: float | None,
    oi_change_24h: float | None,
) -> float:
    """OI velocity: how OI moves relative to price. [-1, 1].

    Interpretation:
      price ↑ + OI ↑ → new longs opening → bullish momentum (positive)
      price ↑ + OI ↓ → shorts closing   → weak rally / short squeeze (positive, weaker)
      price ↓ + OI ↑ → new shorts opening → bearish momentum (negative)
      price ↓ + OI ↓ → longs closing      → weak decline / long liquidation (negative, weaker)

    When price and OI move in the same direction → strong signal.
    When they diverge → weaker signal (mean-reversion likely).

    Returns 0.0 if either input is None.
    """
    if price_change_24h is None or oi_change_24h is None:
        return 0.0

    # Normalize both to [-1, 1] with ±20% saturation
    p = max(-1.0, min(1.0, price_change_24h / 0.20))
    o = max(-1.0, min(1.0, oi_change_24h / 0.20))

    # Same direction = momentum, opposite = mean-reversion
    # The dot product captures this naturally: p*o > 0 when aligned
    alignment = p * o  # ranges [-1, 1]

    # Magnitude: how big are the moves (bigger = more conviction)
    magnitude = (abs(p) + abs(o)) / 2.0  # ranges [0, 1]

    # Blend: momentum score = alignment × magnitude
    # When both move 20% in same direction → score = 1.0
    # When both move 20% in opposite directions → score = -1.0
    # When one is flat → score ≈ 0
    return max(-1.0, min(1.0, alignment * magnitude))


# ── Crowding gate ────────────────────────────────────────────────────────────

def crowding_gate(
    funding_percentile: float | None,
    oi_change_24h: float | None,
    direction: str,
) -> tuple[bool, str]:
    """Check if the trade direction is overcrowded.

    Returns (blocked: bool, reason: str).
    Blocked = True means the trade should be forced to NEUTRAL.

    Rules:
      |funding_percentile| > 0.85 AND |oi_change_24h| > 10% in the same direction
      → the crowded side is about to face a squeeze or fee bleed.
    """
    if funding_percentile is None or oi_change_24h is None or direction == "NEUTRAL":
        return False, ""

    # LONG crowding: funding pctl > 0.85 (longs paying heavily) + OI expanding
    if direction == "LONG" and funding_percentile > 0.85 and oi_change_24h > 0.10:
        return True, (
            f"crowding_gate(LONG|fund_pctl={funding_percentile:.2f}>0.85,"
            f"oi_24h={oi_change_24h:+.1%}>+10%)"
        )

    # SHORT crowding: funding pctl < 0.15 (shorts paying heavily) + OI expanding
    if direction == "SHORT" and funding_percentile < 0.15 and oi_change_24h < -0.10:
        return True, (
            f"crowding_gate(SHORT|fund_pctl={funding_percentile:.2f}<0.15,"
            f"oi_24h={oi_change_24h:+.1%}<-10%)"
        )

    return False, ""
```

---

## 2. STORAGE: `derivatives` table in `store.py`

```sql
CREATE TABLE IF NOT EXISTS derivatives (
    symbol                 TEXT NOT NULL,
    ts                     TEXT NOT NULL,
    oi_usd                 REAL,
    oi_change_1h           REAL,
    oi_change_24h          REAL,
    funding_rate           REAL,
    funding_8h_avg         REAL,
    funding_percentile_30d REAL,
    positioning_score      REAL,
    oi_velocity            REAL,
    PRIMARY KEY (symbol, ts)
);
```

**Migration** (add to `_connect()` alongside existing migrations):

```python
# Check if derivatives table exists, create if not
conn.execute("""
    CREATE TABLE IF NOT EXISTS derivatives (
        symbol                 TEXT NOT NULL,
        ts                     TEXT NOT NULL,
        oi_usd                 REAL,
        oi_change_1h           REAL,
        oi_change_24h          REAL,
        funding_rate           REAL,
        funding_8h_avg         REAL,
        funding_percentile_30d REAL,
        positioning_score      REAL,
        oi_velocity            REAL,
        PRIMARY KEY (symbol, ts)
    )
""")
conn.commit()
```

**Store function:**

```python
def save_derivatives(snapshot_ts: str, rows: list[dict[str, Any]]) -> None:
    """Persist derivatives snapshots tied to a scan timestamp."""
    with _lock:
        conn = _connect()
        conn.executemany(
            """
            INSERT OR REPLACE INTO derivatives (
                symbol, ts, oi_usd, oi_change_1h, oi_change_24h,
                funding_rate, funding_8h_avg, funding_percentile_30d,
                positioning_score, oi_velocity
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r["symbol"], snapshot_ts,
                    r.get("oi_usd"), r.get("oi_change_1h"), r.get("oi_change_24h"),
                    r.get("funding_rate"), r.get("funding_8h_avg"),
                    r.get("funding_percentile_30d"),
                    r.get("positioning_score"), r.get("oi_velocity"),
                )
                for r in rows
            ],
        )
        conn.commit()


def get_latest_derivatives(symbol: str) -> dict[str, Any] | None:
    """Fetch the most recent derivatives row for a symbol."""
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM derivatives WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
            (symbol,),
        ).fetchone()
    return dict(row) if row else None
```

---

## 3. SIGNAL INTEGRATION — `signals.py` changes

Three insertion points, zero changes to existing gates:

### A. Flow composite (replaces raw `net_volume_flow` in Gate 1b)

```python
# ── In compute_signal_v2, BEFORE Gate 1a ─────────────────────────────────────

from .derivatives import fetch_oi_funding, compute_oi_velocity, compute_positioning_score

deriv = fetch_oi_funding(symbol)
price_change_24h = ind.get("change_24h", 0.0)

oi_vel = compute_oi_velocity(price_change_24h, deriv.oi_change_24h if deriv else None)
pos_score = compute_positioning_score(
    deriv.oi_change_24h if deriv else None,
    deriv.funding_percentile_30d if deriv else None,
)

# Flow composite: blend volume flow with OI velocity
# Weights: 70% spot volume (faster, cheaper), 30% derivatives (structural)
FLOW_SPOT_WEIGHT = 0.70
FLOW_DERIV_WEIGHT = 0.30
flow_composite = FLOW_SPOT_WEIGHT * flow + FLOW_DERIV_WEIGHT * oi_vel
```

Then Gate 1b uses `flow_composite` instead of raw `flow`:

```python
# Gate 1b: directional composite confirmation (was: net_volume_flow only)
if (long_bias and flow_composite <= 0) or ((not long_bias) and flow_composite >= 0):
    return _neutral_v2(symbol, entry, grad,
                       f"composite_gate(flow={flow_composite:.2f} vs trend={'LONG' if long_bias else 'SHORT'})",
                       {"gradient_trend": grad, "net_volume_flow": flow,
                        "oi_velocity": oi_vel, "flow_composite": flow_composite})
```

### B. Crowding gate (after pattern multiplier, before final output)

```python
# ── After the pattern multiplier section, BEFORE the factors dict ─────────────

# Crowding gate: block if funding + OI show extreme same-side crowding
blocked, crowding_reason = crowding_gate(
    deriv.funding_percentile_30d if deriv else None,
    deriv.oi_change_24h if deriv else None,
    direction,
)
if blocked:
    return _neutral_v2(symbol, entry, grad, crowding_reason, {
        "gradient_trend": grad, "net_volume_flow": flow,
        "oi_velocity": oi_vel, "flow_composite": flow_composite,
        "positioning_score": pos_score,
        "funding_percentile": deriv.funding_percentile_30d if deriv else None,
    })
```

### C. Confidence modifier (final step, after all gates pass)

```python
# ── Confidence modifier based on positioning alignment ────────────────────────

def _positioning_confidence_mod(
    confidence: int,
    direction: str,
    positioning_score: float,
    flow_composite: float,
) -> int:
    """Adjust confidence based on derivatives alignment.

    Rules:
      1. Strong positioning disagreement → haircut (crowded against us)
      2. Strong agreement → boost (flow + positioning align)
      3. Neutral positioning → no change

    Max haircut: -15 points.  Max boost: +10 points.
    Never drops confidence below 10 (already passed all gates).
    """
    if direction == "LONG":
        align = positioning_score  # positive = bullish crowding
    else:
        align = -positioning_score  # invert for SHORT

    # Strong disagreement: positioning is against us
    if align < -0.4:
        haircut = int(round(15 * min(1.0, abs(align) / 0.6)))
        return max(10, confidence - haircut)

    # Strong agreement: positioning confirms our direction
    if align > 0.4:
        boost = int(round(10 * min(1.0, align / 0.6)))
        return min(100, confidence + boost)

    return confidence
```

Applied in `compute_signal_v2` just before the return:

```python
confidence = _positioning_confidence_mod(confidence, direction, pos_score, flow_composite)
```

### D. Factors dict (add derivatives fields to the payload for storage)

```python
factors = {
    # ... existing fields ...
    "oi_velocity": oi_vel,
    "flow_composite": flow_composite,
    "positioning_score": pos_score,
    "funding_percentile": deriv.funding_percentile_30d if deriv else None,
    "oi_change_24h": deriv.oi_change_24h if deriv else None,
    "funding_rate": deriv.funding_rate if deriv else None,
}
```

---

## 4. TRADE-LEVEL INTEGRATION — `service.py`

Store derivatives snapshot at trade open, link to trade close:

```sql
ALTER TABLE trades ADD COLUMN deriv_snapshot_ts TEXT;
ALTER TABLE trades ADD COLUMN positioning_score REAL;
ALTER TABLE trades ADD COLUMN crowding_flag INTEGER DEFAULT 0;
```

In `open_trade()`:
```python
# After storing the trade, save the derivatives snapshot timestamp
deriv = fetch_oi_funding(symbol)
if deriv:
    store.save_derivatives(scan_ts, [asdict(deriv)])
    store.update_trade_deriv(trade_id, scan_ts, compute_positioning_score(...))
```

In `close_trade()` — use `regime_slippage` from Phase B (already designed) and add `crowding_flag` to `trade_regime` for the regime analysis.

---

## 5. BACKTEST SCENARIOS — Calm / Typical / Stressed

The derivatives overlay doesn't need its own scenario scaling (the slippage model from Phase B handles that). Instead, the three scenarios test **how the crowding gate and confidence modifiers affect trade outcomes**:

| Scenario | Crowding gate | Positioning mod | What it tests |
|----------|--------------|-----------------|---------------|
| `baseline` | OFF | OFF | Pure strategy edge, no derivatives overlay |
| `typical` | ON | ON (full coefficients) | Production: crowding blocks + confidence adjustments active |
| `aggressive` | ON (lower thresholds) | ON (×1.5 boost/haircut) | Stress test: how many trades does the overlay kill? Does edge improve? |

**Implementation — add a config toggle:**

```python
# config.py
DERIVATIVES_OVERLAY: bool = os.getenv("DERIVATIVES_OVERLAY", "true").lower() == "true"
CROWDING_GATE_ENABLED: bool = os.getenv("CROWDING_GATE", "true").lower() == "true"
POSITIONING_MOD_ENABLED: bool = os.getenv("POSITIONING_MOD", "true").lower() == "true"

# Scenario presets
CROWDING_FUNDING_PCTL: float = float(os.getenv("CROWDING_FUNDING_PCTL", "0.85"))
CROWDING_OI_THRESHOLD: float = float(os.getenv("CROWDING_OI_THRESHOLD", "0.10"))
```

Set environment to run each scenario:
```bash
# Baseline
DERIVATIVES_OVERLAY=false uv run crypto-dashboard

# Typical (default)
DERIVATIVES_OVERLAY=true uv run crypto-dashboard

# Aggressive
DERIVATIVES_OVERLAY=true CROWDING_FUNDING_PCTL=0.75 CROWDING_OI_THRESHOLD=0.05 uv run crypto-dashboard
```

**After n≥50 closed trades per scenario:**

```sql
-- Compare net R by scenario
SELECT deriv_overlay_scenario, AVG(net_r_score), COUNT(*), 
       SUM(CASE WHEN r_multiple < 0 THEN 1 ELSE 0 END) AS losses
FROM trades t
LEFT JOIN trade_regime tr ON t.id = tr.id
WHERE net_r_score IS NOT NULL
GROUP BY deriv_overlay_scenario;

-- Compare crowding gate hit rate
SELECT crowding_flag, AVG(net_r_score), COUNT(*)
FROM trades t
WHERE net_r_score IS NOT NULL
GROUP BY crowding_flag;
```

---

## 6. RATE BUDGET SUMMARY

| Operation | Requests/coin | Coins | Total per scan | Budget |
|-----------|-------------|-------|---------------|--------|
| `premiumIndex` | 1 | ~50 | 50 | 1200/min |
| `fundingRate` | 1 | ~50 | 50 | " |
| `openInterest` | 1 | ~50 | 50 | " |
| `openInterestHist` | 1 | ~50 | 50 | " |
| **Total** | **4** | | **~200** | **1200/min** |

With 4 parallel requests per coin and 50 coins: ~10 seconds worst case, typical <2s with HTTP keepalive.

---

## SUMMARY OF CHANGES

| File | Change | Lines | Risk |
|------|--------|-------|------|
| `derivatives.py` (new) | fetch + scoring + crowding gate | ~120 | None — additive |
| `store.py` | `derivatives` table + migration + save/get | ~30 | None — additive |
| `config.py` | 4 env vars: overlay, gate, mod, thresholds | ~6 | None — additive |
| `signals.py` | Flow composite, crowding gate, confidence mod, factors dict | ~40 | Gate *reduces* trades, never adds — conservative |
| `service.py` | `deriv_snapshot_ts`, `positioning_score`, `crowding_flag` on trades | ~15 | None — audit trail only |
| `signals.py` `_apply_regime_guard` | Unchanged | 0 | — |

**Net effect on trade count:** The crowding gate will block ~5-15% of trades (those in extreme same-side funding + OI expansion). The confidence modifier will reduce confidence on ~10-20% of trades. No new entries are ever created — this is purely subtractive.

Want me to implement this now, or keep it as design spec?