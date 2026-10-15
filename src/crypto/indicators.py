"""Technical indicator computation on pandas OHLCV DataFrames.

Engine v2: adds Stochastic RSI, direction-aware volume pressure, and a
continuous gradient-trend score to the original battery (RSI, MACD, ATR,
Bollinger, EMA stack). All functions are deterministic and vectorized.

Legacy output keys from v1 are retained in compute_all() so downstream
consumers keep working until the signal layer migrates to the new fields.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

ENGINE_VERSION = "2.0"

TREND_FAST, TREND_MID, TREND_SLOW = 20, 50, 200
SLOPE_BARS = 5
SEP_WEIGHT, SLOPE_WEIGHT = 0.5, 0.5
PRESSURE_LOOKBACK = 20


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, pd.NA)
    return (100 - (100 / (1 + rs))).fillna(50.0)


def stochastic_rsi(
    close: pd.Series,
    rsi_period: int = 14,
    stoch_period: int = 14,
    k_smooth: int = 3,
    d_smooth: int = 3,
) -> pd.DataFrame:
    """Stochastic RSI (14, 14, 3, 3): Stochastic applied to Wilder RSI.

    %K = SMA(stoch_raw, k_smooth), %D = SMA(%K, d_smooth).
    Warm-up NaNs are filled neutral (0.5) so scoring is never biased by
    missing history.
    """
    r = rsi(close, rsi_period)
    lo = r.rolling(stoch_period).min()
    hi = r.rolling(stoch_period).max()
    raw = (r - lo) / (hi - lo).replace(0.0, pd.NA)
    k = raw.rolling(k_smooth).mean()
    d = k.rolling(d_smooth).mean()
    return pd.DataFrame({"k": k.fillna(0.5), "d": d.fillna(0.5)})


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "histogram": line - sig})


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def bollinger(close: pd.Series, window: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    mid = sma(close, window)
    std = close.rolling(window=window).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    percent_b = (close - lower) / (upper - lower).replace(0.0, pd.NA)
    return pd.DataFrame(
        {"upper": upper, "middle": mid, "lower": lower, "percent_b": percent_b.fillna(0.5)}
    )


def directional_pressure(df: pd.DataFrame, lookback: int = PRESSURE_LOOKBACK) -> dict[str, float]:
    """Direction-aware volume: split each bar's volume into buying/selling
    pressure by close-vs-open. Doji bars split half/half so totals conserve.

    Returns ratios over the trailing `lookback` bars:
      buy_vol_ratio, sell_vol_ratio ∈ [0,1] (sum to 1),
      net_volume_flow = buy - sell ∈ [-1, +1].
    """
    o, c, v = df["Open"], df["Close"], df["Volume"]
    delta = c - o
    up = v * (delta > 0) + 0.5 * v * (delta == 0)
    dn = v * (delta < 0) + 0.5 * v * (delta == 0)
    win_up = float(up.tail(lookback).sum())
    win_dn = float(dn.tail(lookback).sum())
    total = win_up + win_dn
    if total <= 0:
        return {"buy_vol_ratio": 0.5, "sell_vol_ratio": 0.5, "net_volume_flow": 0.0}
    buy = win_up / total
    return {
        "buy_vol_ratio": round(buy, 4),
        "sell_vol_ratio": round(1 - buy, 4),
        "net_volume_flow": round(2 * buy - 1, 4),
    }


def _tanh_last(series: pd.Series) -> float:
    x = series.dropna()
    return float(np.tanh(x.iloc[-1])) if len(x) else 0.0


def gradient_trend(
    df: pd.DataFrame,
    fast: int = TREND_FAST,
    mid: int = TREND_MID,
    slow: int = TREND_SLOW,
    slope_bars: int = SLOPE_BARS,
) -> tuple[float, dict[str, float]]:
    """Continuous trend score ∈ [-1, +1] replacing binary EMA-stack steps.

    Components (all tanh-normalized, expressed in ATR units so the score is
    volatility-invariant across assets):
      sep_fast_mid : tanh((EMA_f - EMA_m) / ATR)
      sep_mid_slow : tanh((EMA_m - EMA_s) / ATR)
      slope_fast   : tanh(per-bar EMA_f slope over `slope_bars` / ATR)
      slope_slow   : tanh(per-bar EMA_s slope over `slope_bars` / ATR)
    score = SEP_WEIGHT · mean(separations) + SLOPE_WEIGHT · mean(slopes)
    """
    close = df["Close"]
    a = atr(df).replace(0.0, pd.NA)
    ef, em, es = ema(close, fast), ema(close, mid), ema(close, slow)

    sep_fm = _tanh_last((ef - em) / a)
    sep_ms = _tanh_last((em - es) / a)
    slope_f = _tanh_last((ef.diff(slope_bars) / slope_bars) / a)
    slope_s = _tanh_last((es.diff(slope_bars) / slope_bars) / a)

    score = SEP_WEIGHT * ((sep_fm + sep_ms) / 2) + SLOPE_WEIGHT * ((slope_f + slope_s) / 2)
    components = {
        "sep_fast_mid": round(sep_fm, 4),
        "sep_mid_slow": round(sep_ms, 4),
        "slope_fast": round(slope_f, 4),
        "slope_slow": round(slope_s, 4),
    }
    return max(-1.0, min(1.0, float(score))), components


def compute_all(df: pd.DataFrame) -> dict[str, float]:
    """Latest values of every indicator (v2 superset; v1 keys preserved)."""
    close = df["Close"]
    latest = close.iloc[-1]
    prev_close = close.iloc[-2] if len(close) > 1 else latest

    e20, e50, e200 = ema(close, 20).iloc[-1], ema(close, 50).iloc[-1], ema(close, 200).iloc[-1]
    r = rsi(close).iloc[-1]
    srsi = stochastic_rsi(close).iloc[-1]
    m = macd(close).iloc[-1]
    a = atr(df).iloc[-1]
    bb = bollinger(close).iloc[-1]
    vol_ratio = df["Volume"].iloc[-1] / df["Volume"].tail(20).mean() if len(df) >= 20 else 1.0

    grad, grad_components = gradient_trend(df)
    pressure = directional_pressure(df)

    out: dict[str, float] = {
        "engine_version_at_compute": ENGINE_VERSION,
        "price": float(latest),
        "change_1d": float((latest / prev_close - 1) * 100),
        # legacy v1 keys (consumed by signals.py v1)
        "ema20": float(e20),
        "ema50": float(e50),
        "ema200": float(e200),
        "rsi14": float(r),
        "macd": float(m["macd"]),
        "macd_signal": float(m["signal"]),
        "macd_hist": float(m["histogram"]),
        "atr": float(a),
        "atr_pct": float(a / latest * 100) if latest else 0.0,
        "bb_upper": float(bb["upper"]),
        "bb_lower": float(bb["lower"]),
        "bb_percent_b": float(bb["percent_b"]),
        "volume_ratio_20d": float(vol_ratio),
        "trend_20_50": float(e20 / e50 - 1) * 100,
        "trend_50_200": float(e50 / e200 - 1) * 100,
        # v2 fields
        "stochrsi_k": float(srsi["k"]) * 100,
        "stochrsi_d": float(srsi["d"]) * 100,
        **pressure,
        "gradient_trend": round(grad, 4),
        **{f"grad_{k}": v for k, v in grad_components.items()},
    }
    return out
