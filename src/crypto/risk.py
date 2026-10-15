"""Dynamic ATR risk engine v2.

Replaces fixed 2:1 geometry (v1 weakness #5):

  STOP — anchored to the nearest confirmed local pivot in the trade's
  direction, buffered by STOP_BUFFER_ATR below/above the structure, then
  clamped into [STOP_FLOOR_ATR, STOP_CAP_ATR] × ATR so no stop is ever
  degenerately tight or recklessly wide.

  TARGET — stretched by the ATR volatility percentile over
  ATR_PCTL_LOOKBACK bars: compressed regimes (low percentile) extend the
  multiple toward TP_RR_LOWVOL (breakouts travel), expanded regimes tighten
  it toward TP_RR_HIGHVOL (exhaustion risk caps reward).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from .indicators import atr

PIVOT_LEFT = PIVOT_RIGHT = 2
STOP_BUFFER_ATR = 0.35
STOP_FLOOR_ATR = 1.2
STOP_CAP_ATR = 2.8
TP_RR_LOWVOL = 4.0
TP_RR_HIGHVOL = 2.0
ATR_PCTL_LOOKBACK = 100


@dataclass
class RiskPlan:
    direction: str
    entry: float
    stop_loss: float
    take_profit: float
    risk_distance: float
    reward_distance: float
    rr: float
    stop_basis: str          # structural | floor_clamp | cap_clamp | atr_floor
    pivot_level: float | None
    tp_multiplier: float     # R-multiple applied to risk_distance
    atr: float
    atr_percentile: float    # 0 = compressed, 1 = expanded

    def to_dict(self) -> dict:
        return asdict(self)


def _pivot_lows(low: pd.Series) -> pd.Series:
    win = low.rolling(PIVOT_LEFT + PIVOT_RIGHT + 1, center=True).min()
    return low.where(low == win).dropna()


def _pivot_highs(high: pd.Series) -> pd.Series:
    win = high.rolling(PIVOT_LEFT + PIVOT_RIGHT + 1, center=True).max()
    return high.where(high == win).dropna()


def atr_percentile(df: pd.DataFrame, lookback: int = ATR_PCTL_LOOKBACK) -> float:
    """Rank of the latest ATR within its own recent history: 0..1."""
    a = atr(df).dropna()
    if a.empty:
        return 0.5
    window = a.tail(lookback)
    return float((window <= a.iloc[-1]).mean())


def tp_multiplier(df: pd.DataFrame) -> float:
    """Volatility-stretched reward multiple (linear map on ATR percentile)."""
    p = atr_percentile(df)
    return round(TP_RR_LOWVOL + (TP_RR_HIGHVOL - TP_RR_LOWVOL) * p, 3)


def build_risk_plan(df: pd.DataFrame, direction: str, entry: float | None = None) -> RiskPlan:
    """Full bracket for one setup from OHLCV structure.

    LONG: stop below nearest confirmed pivot low; SHORT mirrored above the
    nearest confirmed pivot high. `entry` defaults to the last close.
    """
    d = direction.upper()
    if d not in ("LONG", "SHORT"):
        raise ValueError(f"direction must be LONG or SHORT, got {direction!r}")

    close = df["Close"]
    entry = float(entry if entry is not None else close.iloc[-1])
    a = float(atr(df).iloc[-1])
    if a <= 0 or entry <= 0:
        raise ValueError("degenerate OHLCV: ATR and price must be positive")

    if d == "LONG":
        pivots = _pivot_lows(df["Low"])
        below = pivots[pivots < entry]
        raw = float(below.max()) if not below.empty else None
        basis = "structural" if raw is not None else "atr_floor"
        stop = raw - STOP_BUFFER_ATR * a if raw is not None else entry - STOP_FLOOR_ATR * a
        if entry - stop < STOP_FLOOR_ATR * a:
            stop, basis = entry - STOP_FLOOR_ATR * a, ("floor_clamp" if raw is not None else "atr_floor")
        elif entry - stop > STOP_CAP_ATR * a:
            stop, basis = entry - STOP_CAP_ATR * a, "cap_clamp"
        risk = entry - stop
        mult = tp_multiplier(df)
        take = entry + mult * risk
    else:
        pivots = _pivot_highs(df["High"])
        above = pivots[pivots > entry]
        raw = float(above.min()) if not above.empty else None
        basis = "structural" if raw is not None else "atr_floor"
        stop = raw + STOP_BUFFER_ATR * a if raw is not None else entry + STOP_FLOOR_ATR * a
        if stop - entry < STOP_FLOOR_ATR * a:
            stop, basis = entry + STOP_FLOOR_ATR * a, ("floor_clamp" if raw is not None else "atr_floor")
        elif stop - entry > STOP_CAP_ATR * a:
            stop, basis = entry + STOP_CAP_ATR * a, "cap_clamp"
        risk = stop - entry
        mult = tp_multiplier(df)
        take = entry - mult * risk

    reward = abs(take - entry)
    return RiskPlan(
        direction=d,
        entry=round(entry, 8),
        stop_loss=round(stop, 8),
        take_profit=round(take, 8),
        risk_distance=round(risk, 8),
        reward_distance=round(reward, 8),
        rr=round(mult, 3),
        stop_basis=basis,
        pivot_level=round(raw, 8) if raw is not None else None,
        tp_multiplier=mult,
        atr=round(a, 8),
        atr_percentile=round(atr_percentile(df), 4),
    )
