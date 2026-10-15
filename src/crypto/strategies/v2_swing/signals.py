"""v2_swing strategy — frozen multi-factor composite signal engine (v2).

Relocated verbatim from the top-level signals.py so the strategy runs in
parallel with v3_scalp on the same dashboard without cross-contamination.

ENGINE_VERSION=v2: low-noise layered matrix —
  1. Trend/Volume gate : |gradient_trend| >= TREND_GATE_MIN AND net volume
     flow agrees with trend direction, else NEUTRAL (patterns never run).
  2. Pattern multiplier: qualifying same-side candlestick pattern with
     strength >= PATTERN_STRENGTH_MIN multiplies base confidence.
  3. StochRSI trigger  : %K/%D alignment required for execution timing;
     fresh crossovers are reported but not required.
Passing setups receive structural stops and volatility-stretched targets
from risk.py, injected verbatim into the payload.

The v1 path is preserved for the backtest shim; the strategy entry point
`compute_signal` always evaluates engine logic exactly as shipped.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

from crypto import patterns as _patterns
from crypto import risk as _risk
from crypto.indicators import stochastic_rsi
from crypto import config as _cfg

logger = logging.getLogger("crypto.signals.v2_swing")

ENGINE_ENV_VAR = "ENGINE_VERSION"

WEIGHTS = {"trend": 0.30, "momentum": 0.30, "volatility": 0.15, "volume": 0.15, "market": 0.10}

LONG_THRESHOLD = 0.15
SHORT_THRESHOLD = -0.15
ATR_SL_MULT = 1.5
ATR_TP_MULT = 3.0

TREND_GATE_MIN = 0.35
PATTERN_STRENGTH_MIN = 0.75
PATTERN_CONF_MULTIPLIER = 1.25


def _clip(x: float) -> float:
    return max(-1.0, min(1.0, x))


def active_engine_version() -> str:
    """Resolve ENGINE_VERSION env/config; anything other than v2 falls back
    to v1 (safe default keeps the active architecture running)."""
    raw = os.getenv(ENGINE_ENV_VAR, "v1").strip().lower().lstrip("v")
    return "v2" if raw == "2" else "v1"


# ---------------------------------------------------------------------------
# v1 legacy framework — preserved verbatim
# ---------------------------------------------------------------------------


@dataclass
class FactorScores:
    trend: float
    momentum: float
    volatility: float
    volume: float
    market: float


@dataclass
class Signal:
    symbol: str
    direction: str
    confidence: int
    score: float
    factors: FactorScores
    entry: float
    stop_loss: float | None
    take_profit: float | None
    rationale: str = ""
    engine_version_at_compute: str = "v1"


def factor_trend(ind: dict) -> float:
    score = 0.0
    if ind["ema20"] > ind["ema50"]:
        score += 0.5
    else:
        score -= 0.5
    if ind["ema50"] > ind["ema200"]:
        score += 0.5
    else:
        score -= 0.5
    score += _clip(ind["trend_20_50"] / 2.0) * 0.0  # informational only
    return _clip(score)


def factor_momentum(ind: dict) -> float:
    rsi = ind["rsi14"]
    if rsi < 30:
        score = 0.8  # oversold -> mean reversion long bias
    elif rsi > 70:
        score = -0.8  # overbought
    else:
        score = (50 - rsi) / 50.0 * 0.4
    hist = ind["macd_hist"]
    score += 0.6 if hist > 0 else -0.6
    return _clip(score)


def factor_volatility(ind: dict) -> float:
    pct_b = ind["bb_percent_b"]
    score = -pct_b
    if ind["atr_pct"] < 2.0:
        score += 0.2
    return _clip(score)


def factor_volume(ind: dict) -> float:
    ratio = ind["volume_ratio_20d"]
    if ratio >= 1.5:
        return 0.5
    if ratio >= 1.0:
        return 0.2
    return -0.2


def factor_market(market: dict) -> float:
    change_24h = market.get("change_24h") or 0.0
    change_7d = market.get("change_7d") or change_24h
    return _clip((change_24h * 0.02) + (change_7d * 0.01))


# ---------------------------------------------------------------------------
# v1 scoring path
# ---------------------------------------------------------------------------


def compute_signal_v1(symbol: str, ind: dict, market: dict | None = None) -> Signal:
    market = market or {}
    factors = FactorScores(
        trend=factor_trend(ind),
        momentum=factor_momentum(ind),
        volatility=factor_volatility(ind),
        volume=factor_volume(ind),
        market=factor_market(market),
    )
    score = sum(WEIGHTS[k] * getattr(factors, k) for k in WEIGHTS)
    score = _clip(score)

    if score >= LONG_THRESHOLD:
        direction = "LONG"
    elif score <= SHORT_THRESHOLD:
        direction = "SHORT"
    else:
        direction = "NEUTRAL"

    confidence = int(round(abs(score) * 100))
    entry = ind["price"]
    atr = ind["atr"]
    if direction == "LONG":
        stop_loss = entry - ATR_SL_MULT * atr
        take_profit = entry + ATR_TP_MULT * atr
    elif direction == "SHORT":
        stop_loss = entry + ATR_SL_MULT * atr
        take_profit = entry - ATR_TP_MULT * atr
    else:
        stop_loss = None
        take_profit = None

    return Signal(
        symbol=symbol,
        direction=direction,
        confidence=confidence,
        score=score,
        factors=factors,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        engine_version_at_compute="v1",
    )


# ---------------------------------------------------------------------------
# v2 low-noise path
# ---------------------------------------------------------------------------


@dataclass
class V2Signal:
    symbol: str
    direction: str                      # LONG | SHORT | NEUTRAL
    confidence: int
    score: float                        # signed gradient_trend value
    factors: dict[str, Any]
    entry: float
    stop_loss: float | None
    take_profit: float | None
    risk_metadata: dict[str, Any]
    trigger: dict[str, Any]
    pattern: dict[str, Any]
    reason: str
    engine_version_at_compute: str


def _stoch_frame(close: pd.Series) -> pd.DataFrame:
    """Isolated for testability."""
    return stochastic_rsi(close)


def stoch_trigger(df: pd.DataFrame, long_bias: bool) -> dict[str, Any]:
    """StochRSI execution-timing state on closed bars.

    aligned     : %K on the correct side of %D for the bias
    fresh_cross : crossover fired on the last bar in the bias direction
    """
    s = _stoch_frame(df["Close"])
    k_now, d_now = float(s["k"].iloc[-1]), float(s["d"].iloc[-1])
    k_prev, d_prev = float(s["k"].iloc[-2]), float(s["d"].iloc[-2])
    if long_bias:
        aligned = k_now > d_now
        fresh = k_prev <= d_prev and k_now > d_now
    else:
        aligned = k_now < d_now
        fresh = k_prev >= d_prev and k_now < d_now
    return {
        "k": round(k_now, 3),
        "d": round(d_now, 3),
        "aligned": bool(aligned),
        "fresh_cross": bool(fresh),
    }


def _neutral_v2(symbol: str, entry: float, score: float, reason: str, extra: dict[str, Any]) -> V2Signal:
    return V2Signal(
        symbol=symbol,
        direction="NEUTRAL",
        confidence=0,
        score=round(score, 4),
        factors={"gates_failed": reason, **extra},
        entry=round(entry, 8),
        stop_loss=None,
        take_profit=None,
        risk_metadata={},
        trigger={},
        pattern={},
        reason=reason,
        engine_version_at_compute="v2",
    )


def compute_signal_v2(symbol: str, ind: dict, df: pd.DataFrame, market: dict | None = None) -> V2Signal:
    close = df["Close"]
    entry = float(ind.get("price", close.iloc[-1]))
    grad = float(ind["gradient_trend"])
    flow = float(ind.get("net_volume_flow", 0.0))

    # Gate 1a: trend strength floor
    if abs(grad) < TREND_GATE_MIN:
        return _neutral_v2(symbol, entry, grad, f"trend_gate(|{grad:.2f}|<{TREND_GATE_MIN})",
                           {"gradient_trend": grad, "net_volume_flow": flow})

    long_bias = grad > 0

    # Gate 1b: directional volume confirmation
    if (long_bias and flow <= 0) or ((not long_bias) and flow >= 0):
        return _neutral_v2(symbol, entry, grad,
                           f"volume_gate(flow={flow:.2f} vs trend={'LONG' if long_bias else 'SHORT'})",
                           {"gradient_trend": grad, "net_volume_flow": flow})

    # Trigger: StochRSI timing inside the validated trend
    trig = stoch_trigger(df, long_bias)
    if not trig["aligned"]:
        return _neutral_v2(symbol, entry, grad, "trigger_waiting(stochrsi_unaligned)",
                           {"gradient_trend": grad, "net_volume_flow": flow, "trigger": trig})

    direction = "LONG" if long_bias else "SHORT"
    side = "bullish" if long_bias else "bearish"

    # Pattern multiplier (same-side detector on last closed bar)
    p_strength = float(ind.get(f"pat_{side}_strength", 0.0))
    p_any_flag = any(
        bool(ind.get(f"pat_{side}_{name}", False))
        for name in ("engulfing", "pinbar", "sweep")
    )
    multiplied = p_any_flag and p_strength >= PATTERN_STRENGTH_MIN
    base_conf = int(round(abs(grad) * 100))
    confidence = min(100, int(round(base_conf * (PATTERN_CONF_MULTIPLIER if multiplied else 1))))

    # Coherent risk logs from risk.py
    plan = _risk.build_risk_plan(df, direction, entry=entry)

    factors = {
        "gradient_trend": grad,
        "grad_components": {k: ind.get(f"grad_{k}") for k in
                            ("sep_fast_mid", "sep_mid_slow", "slope_fast", "slope_slow")},
        "net_volume_flow": flow,
        "buy_vol_ratio": ind.get("buy_vol_ratio"),
        "sell_vol_ratio": ind.get("sell_vol_ratio"),
        "rsi14": ind.get("rsi14"),
        "stochrsi_k": ind.get("stochrsi_k"),
        "stochrsi_d": ind.get("stochrsi_d"),
        "gates_passed": ["trend_gate", "volume_gate", "trigger"],
    }
    return V2Signal(
        symbol=symbol,
        direction=direction,
        confidence=confidence,
        score=round(grad, 4),
        factors=factors,
        entry=plan.entry,
        stop_loss=plan.stop_loss,
        take_profit=plan.take_profit,
        risk_metadata={
            "stop_basis": plan.stop_basis,
            "pivot_level": plan.pivot_level,
            "rr": plan.rr,
            "tp_multiplier": plan.tp_multiplier,
            "atr_percentile": plan.atr_percentile,
            "atr": plan.atr,
            "risk_distance": plan.risk_distance,
            "reward_distance": plan.reward_distance,
        },
        trigger={**trig, "side": side},
        pattern={
            "side": side,
            "detected": bool(p_any_flag),
            "strength": p_strength,
            "multiplied": bool(multiplied),
            "threshold": PATTERN_STRENGTH_MIN,
            "multiplier": PATTERN_CONF_MULTIPLIER if multiplied else 1.0,
        },
        reason="",
        engine_version_at_compute="v2",
    )


# ---------------------------------------------------------------------------
# dispatcher + serialization
# ---------------------------------------------------------------------------


def compute_signal(
    symbol: str,
    ind: dict,
    market: dict | None = None,
    df: pd.DataFrame | None = None,
    engine_version: str | None = None,
) -> Signal | V2Signal:
    version = (engine_version or active_engine_version()).lower()
    if version in ("v2", "2"):
        if df is None:
            raise ValueError("engine v2 requires the OHLCV DataFrame (df=...)")
        raw = compute_signal_v2(symbol, ind, df, market)
    else:
        raw = compute_signal_v1(symbol, ind, market)
    return _apply_regime_guard(raw)


def _apply_regime_guard(sig: Signal | V2Signal) -> Signal | V2Signal:
    """Suppress SHORT signals under SPOT execution mode."""
    if _cfg.execution_mode() == "SPOT" and sig.direction == "SHORT":
        logger.info("[REGIME GUARD] Short signal suppressed under SPOT configuration.")
        if isinstance(sig, V2Signal):
            return V2Signal(
                symbol=sig.symbol,
                direction="NEUTRAL",
                confidence=0,
                score=sig.score,
                factors={**sig.factors, "regime_suppressed": "SHORT→NEUTRAL (SPOT)"},
                entry=sig.entry,
                stop_loss=None,
                take_profit=None,
                risk_metadata=sig.risk_metadata,
                trigger=sig.trigger,
                pattern=sig.pattern,
                reason=sig.reason or "regime_guard(SPOT_SHORT)",
                engine_version_at_compute=sig.engine_version_at_compute,
            )
        return Signal(
            symbol=sig.symbol,
            direction="NEUTRAL",
            confidence=0,
            score=sig.score,
            factors=sig.factors,
            entry=sig.entry,
            stop_loss=None,
            take_profit=None,
            rationale=sig.rationale or "regime_guard(SPOT_SHORT)",
            engine_version_at_compute=sig.engine_version_at_compute,
        )
    return sig


def signal_to_dict(signal: Signal | V2Signal) -> dict:
    d = asdict(signal)
    if isinstance(d.get("factors"), FactorScores):
        d["factors"] = asdict(signal.factors)
    return d


# ---------------------------------------------------------------------------
# strategy entry point (called by the service layer)
# ---------------------------------------------------------------------------

STRATEGY = "v2_swing"


def compute_for_strategy(symbol: str, ind: dict, df: pd.DataFrame, market: dict | None = None) -> V2Signal:
    """v2_swing entry: evaluate the frozen v2 engine and record its version tag."""
    sig = compute_signal(symbol, ind, market=market, df=df, engine_version="v2")
    if hasattr(sig, "engine_version_at_compute") and sig.engine_version_at_compute:
        return sig  # type: ignore[return-value]
    return _apply_regime_guard(compute_signal_v2(symbol, ind, df, market))
