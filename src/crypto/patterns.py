"""Quantified candlestick pattern detection — pure OHLC mathematics.

Detectors (all vectorized, degenerate-candle safe):
  - Engulfing: strict price-barrier body engulfment of the prior opposite
    candle (open/close must violate the prior body's boundaries; no visual
    interpretation involved).
  - Pinbar: rejection tail >= PIN_TAIL_BODY x real body, opposing wick capped,
    close positioned in the rejection direction.
  - Liquidity sweep: wick pierces the prior SWEEP_LOOKBACK extreme and the
    candle closes back beyond it with a qualifying rejection tail.

Strength scores are normalized magnitudes in [0, 1] for downstream weighting.
"""

from __future__ import annotations

import pandas as pd

PIN_TAIL_BODY = 2.5          # min tail / body multiple for a pinbar
PIN_CLOSE_POS_MIN = 0.60     # pinbar must close in its rejection direction's 40% zone
OPPO_WICK_MAX_FRAC = 0.25    # opposing wick ceiling as fraction of total range
SWEEP_LOOKBACK = 10          # bars defining the liquidity pool (prior extreme)
STRENGTH_CAP = 4.0           # tail multiples beyond this add no strength


def _geometry(df: pd.DataFrame) -> dict[str, pd.Series]:
    o, h, l, c = df["Open"], df["High"], df["Low"], df["Close"]
    body = (c - o).abs()
    rng = (h - l).replace(0.0, pd.NA)
    body_top = pd.concat([o, c], axis=1).max(axis=1)
    body_bot = pd.concat([o, c], axis=1).min(axis=1)
    return {
        "o": o, "h": h, "l": l, "c": c,
        "body": body, "rng": rng,
        "up_wick": h - body_top,
        "dn_wick": body_bot - l,
        "close_pos": (c - l) / rng,
    }


def engulfing(df: pd.DataFrame) -> pd.DataFrame:
    """Strict-engulfment booleans.

    Bullish: prior bear, current bull, current open <= prior close AND
    current close >= prior open (full barrier violation), body strictly larger.
    Bearish mirrored. Zero-range candles never qualify.
    """
    g = _geometry(df)
    o, c, body, rng = g["o"], g["c"], g["body"], g["rng"]

    prev_o, prev_c, prev_body = o.shift(1), c.shift(1), body.shift(1)
    prev_bear = prev_c < prev_o
    prev_bull = prev_c > prev_o

    bull = (
        prev_bear
        & (c > o)
        & (o <= prev_c)
        & (c >= prev_o)
        & (body > prev_body)
        & (rng > 0)
    )
    bear = (
        prev_bull
        & (c < o)
        & (o >= prev_c)
        & (c <= prev_o)
        & (body > prev_body)
        & (rng > 0)
    )
    return pd.DataFrame({"bullish_engulfing": bull.fillna(False),
                         "bearish_engulfing": bear.fillna(False)})


def pinbar(df: pd.DataFrame) -> pd.DataFrame:
    """Rejection-candle booleans + strength.

    Bullish pin: lower wick >= PIN_TAIL_BODY * body, upper wick <=
    OPPO cap fraction of range, close in upper zone of the bar.
    Bearish mirrored. Strength = min(tail/body, STRENGTH_CAP)/STRENGTH_CAP.
    """
    g = _geometry(df)
    body, rng = g["body"], g["rng"]
    up_wick, dn_wick, close_pos = g["up_wick"], g["dn_wick"], g["close_pos"]

    tail_ratio_dn = dn_wick / body.replace(0.0, pd.NA)
    tail_ratio_up = up_wick / body.replace(0.0, pd.NA)

    bull = (
        (tail_ratio_dn >= PIN_TAIL_BODY)
        & ((up_wick / rng).fillna(1.0) <= OPPO_WICK_MAX_FRAC)
        & (close_pos >= PIN_CLOSE_POS_MIN)
        & (rng > 0)
    )
    bear = (
        (tail_ratio_up >= PIN_TAIL_BODY)
        & ((dn_wick / rng).fillna(1.0) <= OPPO_WICK_MAX_FRAC)
        & (close_pos <= 1 - PIN_CLOSE_POS_MIN)
        & (rng > 0)
    )
    strength = pd.concat(
        [tail_ratio_dn.clip(lower=0).div(STRENGTH_CAP).clip(upper=1).where(bull, 0.0),
         tail_ratio_up.clip(lower=0).div(STRENGTH_CAP).clip(upper=1).where(bear, 0.0)],
        axis=1,
    ).max(axis=1)
    return pd.DataFrame({
        "bullish_pinbar": bull.fillna(False),
        "bearish_pinbar": bear.fillna(False),
        "pinbar_strength": strength.fillna(0.0),
    })


def sweep(df: pd.DataFrame) -> pd.DataFrame:
    """Liquidity-sweep booleans: pierce prior SWEEP_LOOKBACK extreme, then
    reject back through it with a qualifying pinbar tail on the same bar."""
    g = _geometry(df)
    l, h, c = g["l"], g["h"], g["c"]
    prior_low = l.shift(1).rolling(SWEEP_LOOKBACK).min()
    prior_high = h.shift(1).rolling(SWEEP_LOOKBACK).max()
    pins = pinbar(df)

    bull = (l < prior_low) & pins["bullish_pinbar"]
    bear = (h > prior_high) & pins["bearish_pinbar"]
    return pd.DataFrame({"bullish_sweep": bull.fillna(False),
                         "bearish_sweep": bear.fillna(False)})


def strengths(df: pd.DataFrame) -> pd.DataFrame:
    """Per-bar, per-side pattern strength in [0, 1] for signal weighting.

    engulfing : body dominance (body / range)
    pinbar    : tail-multiple score (see pinbar())
    sweep     : its qualifying pinbar's tail score
    Side strength = elementwise max across detectors.
    """
    g = _geometry(df)
    body_dom = (g["body"] / g["rng"]).clip(lower=0.0, upper=1.0)
    eng = engulfing(df)
    pb = pinbar(df)
    sw = sweep(df)

    bull = pd.concat(
        [body_dom.where(eng["bullish_engulfing"], 0.0),
         pb["pinbar_strength"].where(pb["bullish_pinbar"], 0.0),
         pb["pinbar_strength"].where(sw["bullish_sweep"], 0.0)],
        axis=1,
    ).max(axis=1)
    bear = pd.concat(
        [body_dom.where(eng["bearish_engulfing"], 0.0),
         pb["pinbar_strength"].where(pb["bearish_pinbar"], 0.0),
         pb["pinbar_strength"].where(sw["bearish_sweep"], 0.0)],
        axis=1,
    ).max(axis=1)
    return pd.DataFrame({"bullish_strength": bull.fillna(0.0),
                         "bearish_strength": bear.fillna(0.0)})


def detect(df: pd.DataFrame) -> pd.DataFrame:
    """All detectors concatenated on one index-aligned frame."""
    out = pd.concat([engulfing(df), pinbar(df), sweep(df)], axis=1)
    return out.fillna(False).astype(bool)


def latest(df: pd.DataFrame, lookback: int = 3) -> dict[str, float]:
    """Pipeline summary: flags + per-side strengths on the last closed bar,
    recency counts over `lookback` bars, and window-max strength.
    Booleans are returned as floats (1.0/0.0) for JSON transport."""
    d = detect(df).tail(lookback)
    s = strengths(df).tail(lookback)
    last_d, last_s = d.iloc[-1], s.iloc[-1]
    out: dict[str, float] = {}
    for col in ("bullish_engulfing", "bearish_engulfing", "bullish_pinbar",
                "bearish_pinbar", "bullish_sweep", "bearish_sweep"):
        out[f"pat_{col}"] = float(bool(last_d[col]))
        out[f"pat_{col}_recent_n"] = float(int(d[col].sum()))
    out["pat_bullish_strength"] = round(float(last_s["bullish_strength"]), 4)
    out["pat_bearish_strength"] = round(float(last_s["bearish_strength"]), 4)
    out["pat_max_strength"] = round(float(s[["bullish_strength", "bearish_strength"]].max().max()), 4)
    return out
