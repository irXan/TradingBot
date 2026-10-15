"""v3_scalp risk plan — tighter than swing.

Scalping entries take small, fast profits and cut losses quickly. The bracket
is a fixed ATR multiple (no pivot anchoring): stop 1.0 x ATR, target 1.5 x ATR
for a clean 1.5R reward/risk. ATR percentile is still logged for regime
context, but it does not stretch the target like the swing engine.

SPOT / halal only — no leverage, no shorting.
"""

from __future__ import annotations

import pandas as pd

from crypto.indicators import atr
from crypto.risk import atr_percentile

STOP_ATR_MULT = 1.0
TP_ATR_MULT = 1.5
ATR_PCTL_LOOKBACK = 100


class ScalpRiskPlan:
    __slots__ = (
        "direction", "entry", "stop_loss", "take_profit",
        "risk_distance", "reward_distance", "rr", "atr", "atr_percentile",
    )

    def __init__(self, direction: str, df: pd.DataFrame, entry: float | None = None) -> None:
        d = direction.upper()
        if d not in ("LONG", "SHORT"):
            raise ValueError(f"direction must be LONG or SHORT, got {direction!r}")
        close = df["Close"]
        entry = float(entry if entry is not None else close.iloc[-1])
        a = float(atr(df).iloc[-1])
        if a <= 0 or entry <= 0:
            raise ValueError("degenerate OHLCV: ATR and price must be positive")
        risk = STOP_ATR_MULT * a
        reward = TP_ATR_MULT * a
        if d == "LONG":
            stop, take = entry - risk, entry + reward
        else:
            stop, take = entry + risk, entry - reward
        self.direction = d
        self.entry = round(entry, 8)
        self.stop_loss = round(stop, 8)
        self.take_profit = round(take, 8)
        self.risk_distance = round(risk, 8)
        self.reward_distance = round(reward, 8)
        self.rr = TP_ATR_MULT
        self.atr = round(a, 8)
        self.atr_percentile = round(atr_percentile(df, ATR_PCTL_LOOKBACK), 4)

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "stop_basis": "atr",
            "tp_multiplier": self.rr,
            "atr": self.atr,
            "atr_percentile": self.atr_percentile,
            "risk_distance": self.risk_distance,
            "reward_distance": self.reward_distance,
        }
