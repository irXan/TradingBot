"""v3_scalp strategy — short-horizon pullback scalp on 5m/15m candles.

Halal, SPOT-only: no leverage, no shorting (SHORT is suppressed by the regime
guard). Takes tight profits and cuts losses fast.

Entry rules (all must hold on the last closed bar):
  LONG-BIAS trend : EMA8 > EMA21               (short-term uptrend intact)
  Pullback cooled  : RSI14 in [40, 60]          (not overbought/oversold)
  Reclaim         : close >= EMA8               (bounce back onto the fast EMA)
  Participation   : volume_ratio_20d >= 1.0     (volume confirms the move)

Confidence is a small bounded composite of the EMA spread, RSI position and
volume ratio. Risk bracket is the tight 1.0R stop / 1.5R target from risk.py.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

from crypto import config as _cfg
from crypto.indicators import compute_all
from .risk import ScalpRiskPlan

RSI_PULLBACK_LO = 40.0
RSI_PULLBACK_HI = 60.0
VOL_RATIO_MIN = 1.0
CONF_HI = 95
CONF_LO = 25


@dataclass
class ScalpSignal:
    symbol: str
    direction: str
    confidence: int
    score: float
    factors: dict[str, Any]
    entry: float
    stop_loss: float | None
    take_profit: float | None
    risk_metadata: dict[str, Any]
    reason: str
    engine_version_at_compute: str


def _ema(close: pd.Series, span: int) -> float:
    return float(close.ewm(span=span, adjust=False).mean().iloc[-1])


def compute_signal(symbol: str, ind: dict, df: pd.DataFrame, market: dict | None = None) -> ScalpSignal:
    close = df["Close"]
    entry = float(ind.get("price", close.iloc[-1]))
    rsi = float(ind["rsi14"])
    atr = float(ind["atr"])
    vol_ratio = float(ind.get("volume_ratio_20d", 0.0))
    ema8 = _ema(close, 8)
    ema21 = _ema(close, 21)
    ema_spread = (ema8 - ema21) / ema21 * 100.0 if ema21 else 0.0

    factors = {
        "ema8": round(ema8, 8),
        "ema21": round(ema21, 8),
        "ema_spread_pct": round(ema_spread, 4),
        "rsi14": rsi,
        "volume_ratio_20d": vol_ratio,
        "atr_pct": ind.get("atr_pct"),
    }

    long_bias = ema8 > ema21
    pullback = RSI_PULLBACK_LO <= rsi <= RSI_PULLBACK_HI
    reclaim = entry >= ema8
    participation = vol_ratio >= VOL_RATIO_MIN

    passed = {"trend": long_bias, "pullback": pullback, "reclaim": reclaim, "volume": participation}

    if not all(passed.values()):
        return ScalpSignal(
            symbol=symbol,
            direction="NEUTRAL",
            confidence=0,
            score=round(ema_spread, 4),
            factors={**factors, "gates_failed": [k for k, v in passed.items() if not v]},
            entry=round(entry, 8),
            stop_loss=None,
            take_profit=None,
            risk_metadata={},
            reason="scalp_gates",
            engine_version_at_compute="v3",
        )

    # Confidence: EMA spread weight + RSI centrality + volume participation.
    rsi_central = 1.0 - abs(rsi - 50.0) / 50.0
    spread_w = min(1.0, abs(ema_spread) / 1.0)
    vol_w = min(1.0, max(0.0, (vol_ratio - VOL_RATIO_MIN) / 1.5))
    composite = 0.5 * rsi_central + 0.3 * spread_w + 0.2 * vol_w
    confidence = int(round(CONF_LO + composite * (CONF_HI - CONF_LO)))
    confidence = max(CONF_LO, min(CONF_HI, confidence))

    plan = ScalpRiskPlan("LONG", df, entry=entry)

    return ScalpSignal(
        symbol=symbol,
        direction="LONG",
        confidence=confidence,
        score=round(ema_spread, 4),
        factors=factors,
        entry=plan.entry,
        stop_loss=plan.stop_loss,
        take_profit=plan.take_profit,
        risk_metadata=plan.to_dict(),
        reason="scalp_pullback_long",
        engine_version_at_compute="v3",
    )


def signal_to_dict(signal: ScalpSignal) -> dict:
    return asdict(signal)


def compute_for_strategy(symbol: str, ind: dict, df: pd.DataFrame, market: dict | None = None) -> ScalpSignal:
    raw = compute_signal(symbol, ind, df, market)
    if _cfg.execution_mode().upper() == "SPOT" and raw.direction == "SHORT":
        raw.direction = "NEUTRAL"
        raw.confidence = 0
        raw.stop_loss = None
        raw.take_profit = None
        raw.factors = {**raw.factors, "regime_suppressed": "SHORT→NEUTRAL (SPOT)"}
    return raw


class _ScalpIndicators:  # marker used by registry helpers (unused, clarity only)
    pass
