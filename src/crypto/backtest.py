"""Research backtest runner — replays historical OHLCV through the frozen v2
engine bar-by-bar with no look-ahead, logging trades into the same `trades`
schema as live paper trades (tagged source='backtest').

This is RESEARCH ONLY. It does not touch the live forward-test sample.
Signal thresholds, indicator parameters, pattern definitions, and risk rules
are all reused verbatim (compute_signal / build_risk_plan). No logic changes.

Usage:
    uv run python -m crypto.backtest \
        --symbols BTC,ETH,SOL \
        --interval 1h \
        --start 2025-01-01 --end 2025-12-31 \
        --equity 10000 --risk-pct 1.0 \
        --out backtest_results

Look-ahead guarantee:
    - Signals are computed on the CLOSE of bar t using only window [:t+1].
    - Positions enter at the OPEN of bar t+1 (never the signal bar close).
    - Positions are managed bar-by-bar from the entry bar onward; SL checked
      before TP within a bar (conservative, matches service.replay_missed_exits).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import config as _cfg
from . import patterns as _patterns
from . import risk as _risk
from . import store
from .indicators import compute_all
from .indicators import atr, bollinger, directional_pressure, ema, macd, rsi, stochastic_rsi
from .market_data import get_ohlcv_range
from .service import _net_r, _r_multiple
from .signals import compute_signal

logger = logging.getLogger("crypto.backtest")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# Warm-up: enough bars for EMA(200), ATR(14), pivot lookback, and a safety margin.
WARMUP_BARS = 240


@dataclass
class Position:
    symbol: str
    direction: str
    entry: float
    stop_loss: float
    take_profit: float
    size_quote: float
    risk_quote: float
    entry_bar: int
    opened_at: str
    interval: str
    risk_metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "OPEN"
    exit_price: float | None = None
    exit_bar: int | None = None
    closed_at: str | None = None


def _iso(ts: pd.Timestamp) -> str:
    return ts.isoformat()


def run_backtest(
    symbols: list[str],
    interval: str,
    start: str,
    end: str,
    equity: float,
    risk_pct: float,
    out_dir: Path,
    write_db: bool = True,
) -> dict[str, Any]:
    """Run the bar-by-bar backtest across symbols. Returns report data."""
    collected: list[dict[str, Any]] = []
    per_symbol: dict[str, Any] = {}
    start_ts = pd.Timestamp(start, tz="UTC")

    for symbol in symbols:
        logger.info("Backtesting %s (%s, %s → %s)", symbol, interval, start, end)
        t0 = time.monotonic()
        try:
            df = get_ohlcv_range(symbol, interval=interval, start=start, end=end)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping %s (data load failed): %s", symbol, exc)
            continue
        if len(df) <= WARMUP_BARS:
            logger.warning("Skipping %s (only %d bars, need %d+)",
                           symbol, len(df), WARMUP_BARS)
            continue

        trades = _walk(symbol, interval, df, equity, risk_pct, out_dir)
        collected.extend(trades)
        per_symbol[symbol] = {
            "n": len(trades),
            "bars": len(df),
            "seconds": round(time.monotonic() - t0, 2),
        }
        logger.info("  %s: %d closed trades", symbol, len(trades))

    report: dict[str, Any] = {
        "symbols": per_symbol,
        "trades": collected,
        "config": {
            "interval": interval,
            "start": start,
            "end": end,
            "equity": equity,
            "risk_pct": risk_pct,
        },
    }

    if collected:
        if write_db:
            n = store.insert_backtest_trades(collected)
            logger.info("Inserted %d backtest trades into DB (source='backtest')", n)
        _write_csv(out_dir, collected)
        _write_report(out_dir, collected, report)
        _write_json(out_dir, report)
    else:
        logger.warning("No closed trades produced; no CSV/report written.")
    return report


def _walk(
    symbol: str,
    interval: str,
    df: pd.DataFrame,
    equity: float,
    risk_pct: float,
    out_dir: Path,
) -> list[dict[str, Any]]:
    """Bar-by-bar loop for one symbol. Returns closed trade records.

    Uses a precomputed per-bar indicator/pattern grid (causal, vectorized once
    per symbol) so the loop is O(n) instead of O(n²) while producing EXACTLY
    the same per-bar values as recomputing on each growing prefix. Because every
    indicator is causal (EMA/RSI/MACD/ATR/BB/rolling all depend only on past
    bars), value[i] on the full frame equals value[i] of df.iloc[:i+1].
    """
    ind_frame, pat_frame = _precompute(df)
    trades: list[dict[str, Any]] = []
    position: Position | None = None
    n = len(df)
    risk_amount = equity * risk_pct / 100.0

    market = {"symbol": symbol.upper(), "name": None, "change_24h": None, "change_7d": None}

    for i in range(WARMUP_BARS, n):
        # --- Signal on the close of bar i (using only data up to i) ---
        ind = {k: float(v) for k, v in ind_frame.iloc[i].items()}
        if ind["atr"] <= 0 or ind["price"] <= 0:
            continue
        ind.update({k: float(v) for k, v in pat_frame.iloc[i].items()})
        window = df.iloc[: i + 1]
        try:
            sig = compute_signal(symbol, ind, market=market, df=window, engine_version="v2")
        except Exception as exc:  # noqa: BLE001 - keep scanning past a bad bar
            logger.debug("compute_signal failed at bar %d for %s: %s", i, symbol, exc)
            continue

        # --- Manage any open position on bar i (post-signal, pre-entry) ---
        if position is not None:
            closed = _manage_position(position, df, i)
            if closed is not None:
                trades.append(closed)
                position = None

        # --- Open new position at NEXT bar open if actionable ---
        if position is None and i + 1 < n and sig.direction != "NEUTRAL":
            entry_bar = i + 1
            entry = float(df["Open"].iloc[entry_bar])
            if entry <= 0:
                continue
            direction = sig.direction
            try:
                plan = _risk.build_risk_plan(
                    df.iloc[: entry_bar + 1], direction, entry=entry
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("build_risk_plan failed at bar %d for %s: %s",
                             entry_bar, symbol, exc)
                continue
            stop_distance_pct = abs(entry - plan.stop_loss) / entry
            if stop_distance_pct <= 0:
                continue
            size_quote = round(risk_amount / stop_distance_pct, 2)
            position = Position(
                symbol=symbol.upper(),
                direction=direction,
                entry=entry,
                stop_loss=plan.stop_loss,
                take_profit=plan.take_profit,
                size_quote=size_quote,
                risk_quote=round(risk_amount, 2),
                entry_bar=entry_bar,
                opened_at=_iso(df.index[entry_bar]),
                interval=interval,
                risk_metadata=plan.to_dict(),
            )

    # Close any position still open at series end (mark-to-close at last close).
    if position is not None:
        last = n - 1
        trades.append(_close_at(position, df, last, float(df["Close"].iloc[last]), "MANUAL_CLOSE"))

    return trades


def _precompute(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build per-bar indicator + pattern grids in one causal vectorized pass.

    Mirrors `compute_all` / `patterns.latest` per index. All series are causal;
    value[i] equals what compute_all(df.iloc[:i+1]) returns.
    """
    close = df["Close"]
    a = atr(df).replace(0.0, np.nan)
    ef = ema(close, 20)
    em = ema(close, 50)
    es = ema(close, 200)
    r = rsi(close)
    srsi = stochastic_rsi(close)
    m = macd(close)
    bb = bollinger(close)
    vol20 = df["Volume"].rolling(20).mean()

    # Directional pressure: rolling sums; doji split half/half conserves volume.
    delta = close - df["Open"]
    up = df["Volume"] * (delta > 0) + 0.5 * df["Volume"] * (delta == 0)
    dn = df["Volume"] * (delta < 0) + 0.5 * df["Volume"] * (delta == 0)
    win_up = up.rolling(20).sum()
    win_dn = dn.rolling(20).sum()
    total = win_up + win_dn
    buy_ratio = win_up / total.replace(0.0, np.nan)
    flow = 2 * buy_ratio - 1

    # gradient_trend components (tanh-normalized, ATR-invariant) per index.
    def _ts(series: pd.Series) -> pd.Series:
        return np.tanh(series / a)

    sep_fm = _ts(ef - em)
    sep_ms = _ts(em - es)
    slope_f = _ts((ef.diff(5) / 5))
    slope_s = _ts((es.diff(5) / 5))
    grad = 0.5 * ((sep_fm + sep_ms) / 2) + 0.5 * ((slope_f + slope_s) / 2)
    grad = np.clip(grad, -1.0, 1.0)
    grad = grad.fillna(0.0)

    frame = pd.DataFrame(
        {
            "engine_version_at_compute": "2.0",
            "price": close,
            "ema20": ef,
            "ema50": em,
            "ema200": es,
            "rsi14": r,
            "macd": m["macd"],
            "macd_signal": m["signal"],
            "macd_hist": m["histogram"],
            "atr": a,
            "atr_pct": a / close * 100,
            "bb_upper": bb["upper"],
            "bb_lower": bb["lower"],
            "bb_percent_b": bb["percent_b"],
            "volume_ratio_20d": df["Volume"] / vol20.replace(0.0, np.nan),
            "trend_20_50": (ef / em - 1) * 100,
            "trend_50_200": (em / es - 1) * 100,
            "stochrsi_k": srsi["k"] * 100,
            "stochrsi_d": srsi["d"] * 100,
            "buy_vol_ratio": buy_ratio.round(4),
            "sell_vol_ratio": (1 - buy_ratio).round(4),
            "net_volume_flow": flow.round(4),
            "gradient_trend": grad.round(4),
            "grad_sep_fast_mid": sep_fm.round(4),
            "grad_sep_mid_slow": sep_ms.round(4),
            "grad_slope_fast": slope_f.round(4),
            "grad_slope_slow": slope_s.round(4),
        }
    )
    frame = frame.replace([np.inf, -np.inf], np.nan)

    # Patterns per bar: detect + strengths over a trailing 3-bar window.
    det = _patterns.detect(df)
    strg = _patterns.strengths(df)
    cols = ["bullish_engulfing", "bearish_engulfing", "bullish_pinbar",
            "bearish_pinbar", "bullish_sweep", "bearish_sweep"]
    pat = pd.DataFrame(index=df.index)
    for c in cols:
        pat[f"pat_{c}"] = det[c]
        pat[f"pat_{c}_recent_n"] = det[c].rolling(3, min_periods=1).sum()
    pat["pat_bullish_strength"] = strg["bullish_strength"].round(4)
    pat["pat_bearish_strength"] = strg["bearish_strength"].round(4)
    pat["pat_max_strength"] = strg[["bullish_strength", "bearish_strength"]].max(axis=1).round(4)
    pat = pat.fillna(0.0)

    return frame, pat

def _manage_position(pos: Position, df: pd.DataFrame, i: int) -> dict[str, Any] | None:
    """Advance `pos` one bar; return a closed-trade record if SL/TP is hit.

    SL checked before TP within the same bar (conservative). Exits AT the level.
    """
    h = df["High"].iloc[i]
    l = df["Low"].iloc[i]
    if pos.direction == "LONG":
        if l <= pos.stop_loss:
            return _close_at(pos, df, i, pos.stop_loss, "SL_HIT")
        if h >= pos.take_profit:
            return _close_at(pos, df, i, pos.take_profit, "TP_HIT")
    else:
        if h >= pos.stop_loss:
            return _close_at(pos, df, i, pos.stop_loss, "SL_HIT")
        if l <= pos.take_profit:
            return _close_at(pos, df, i, pos.take_profit, "TP_HIT")
    return None


def _close_at(pos: Position, df: pd.DataFrame, i: int, level: float, status: str) -> dict[str, Any]:
    r = _r_multiple(pos.direction, pos.entry, pos.stop_loss, level)
    net_r = _net_r(pos.direction, pos.entry, pos.stop_loss, level)
    fee = _cfg.active_taker_fee()
    buf = _cfg.SLIPPAGE_BUFFER
    entry_slip = pos.entry * (1 + buf) if pos.direction == "LONG" else pos.entry * (1 - buf)
    exit_slip = level * (1 - buf) if pos.direction == "LONG" else level * (1 + buf)
    fee_total = entry_slip * fee + exit_slip * fee
    return {
        "opened_at": pos.opened_at,
        "closed_at": _iso(df.index[i]),
        "symbol": pos.symbol,
        "interval": pos.interval,
        "direction": pos.direction,
        "entry": round(pos.entry, 8),
        "stop_loss": round(pos.stop_loss, 8),
        "take_profit": round(pos.take_profit, 8),
        "size_quote": round(pos.size_quote, 2),
        "risk_quote": round(pos.risk_quote, 2),
        "status": status,
        "exit_price": round(level, 8),
        "r_multiple": round(r, 4),
        "net_r_score": round(net_r, 4),
        "entry_slippage": round(entry_slip, 8),
        "exit_slippage": round(exit_slip, 8),
        "fee_paid": round(fee_total, 8),
        "source": "backtest",
        "risk_metadata": pos.risk_metadata,
    }


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

_TRADE_COLS = [
    "id", "opened_at", "closed_at", "symbol", "interval", "direction",
    "entry", "stop_loss", "take_profit", "size_quote", "risk_quote", "status",
    "exit_price", "r_multiple", "net_r_score", "entry_slippage", "exit_slippage",
    "fee_paid", "source",
]


def _write_csv(out_dir: Path, trades: list[dict[str, Any]]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "backtest_trades.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_TRADE_COLS, extrasaction="ignore")
        writer.writeheader()
        for i, t in enumerate(trades, start=1):
            row = {k: t.get(k) for k in _TRADE_COLS}
            row["id"] = i
            writer.writerow(row)
    logger.info("Wrote %s (%d rows)", path, len(trades))


def _write_json(out_dir: Path, report: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "backtest_report.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    logger.info("Wrote %s", path)


def _write_report(out_dir: Path, trades: list[dict[str, Any]], report: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "backtest_report.md"
    closed = [t for t in trades if t.get("r_multiple") is not None]
    rs = [t["r_multiple"] for t in closed]

    wins = sum(1 for r in rs if r > 0)
    losses = sum(1 for r in rs if r <= 0)
    gross_win = sum(r for r in rs if r > 0)
    gross_loss = abs(sum(r for r in rs if r <= 0))
    worst = current = 0
    for r in sorted(closed, key=lambda t: t["closed_at"]):
        rv = r["r_multiple"]
        current = current + 1 if rv <= 0 else 0
        worst = max(worst, current)

    win_rate = wins / len(rs) * 100 if rs else 0.0
    expectancy = sum(rs) / len(rs) if rs else 0.0
    pf = gross_win / gross_loss if gross_loss > 0 else (None if gross_win == 0 else float("inf"))

    lines = [
        "# Backtest Report",
        "",
        f"**Interval:** {report['config']['interval']}  ",
        f"**Range:** {report['config']['start']} → {report['config']['end']}  ",
        f"**Initial equity:** ${report['config']['equity']:,.0f}  ",
        f"**Risk per trade:** {report['config']['risk_pct']}%  ",
        "",
        "## Summary",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total trades | {len(rs)} |",
        f"| Wins / Losses | {wins} / {losses} |",
        f"| Win rate | {win_rate:.1f}% |",
        f"| Expectancy | {expectancy:+.3f} R |",
        f"| Total R | {sum(rs):+.2f} |",
        f"| Profit factor | {('∞' if pf == float('inf') else f'{pf:.2f}' if pf is not None else 'N/A')} |",
        f"| Worst losing streak | {worst} |",
        "",
        "## By Symbol",
        "",
        "| Symbol | Trades | Win rate | Expectancy | PF |",
        "|--------|--------|----------|------------|----|",
    ]

    for symbol, meta in report["symbols"].items():
        sym_trades = [t for t in closed if t["symbol"] == symbol]
        if not sym_trades:
            continue
        s_rs = [t["r_multiple"] for t in sym_trades]
        s_w = sum(1 for r in s_rs if r > 0)
        s_gw = sum(r for r in s_rs if r > 0)
        s_gl = abs(sum(r for r in s_rs if r <= 0))
        s_pf = s_gw / s_gl if s_gl > 0 else (None if s_gw == 0 else float("inf"))
        lines.append(
            f"| {symbol} | {len(s_rs)} | {s_w/len(s_rs)*100:.1f}% | "
            f"{sum(s_rs)/len(s_rs):+.3f} | "
            f"{'∞' if s_pf == float('inf') else f'{s_pf:.2f}' if s_pf is not None else 'N/A'} |"
        )

    lines += [
        "",
        "## By Direction",
        "",
        "| Direction | Trades | Win rate | Expectancy |",
        "|-----------|--------|----------|------------|",
    ]
    for direction in ("LONG", "SHORT"):
        d_trades = [t for t in closed if t["direction"] == direction]
        if not d_trades:
            continue
        d_rs = [t["r_multiple"] for t in d_trades]
        d_w = sum(1 for r in d_rs if r > 0)
        lines.append(
            f"| {direction} | {len(d_rs)} | {d_w/len(d_rs)*100:.1f}% | "
            f"{sum(d_rs)/len(d_rs):+.3f} |"
        )

    # Regime buckets using risk_metadata.atr_percentile when present
    lines += [
        "",
        "## By ATR%-ile Regime",
        "",
        "| Regime | Trades | Win rate | Expectancy |",
        "|--------|--------|----------|------------|",
    ]
    buckets: dict[str, list[dict[str, Any]]] = {"low": [], "mid": [], "high": []}
    for t in closed:
        pct = t.get("risk_metadata", {}).get("atr_percentile")
        if pct is None:
            continue
        if pct < 0.33:
            buckets["low"].append(t)
        elif pct < 0.67:
            buckets["mid"].append(t)
        else:
            buckets["high"].append(t)
    for label, group in buckets.items():
        if not group:
            continue
        g_rs = [t["r_multiple"] for t in group]
        g_w = sum(1 for r in g_rs if r > 0)
        lines.append(
            f"| {label} | {len(g_rs)} | {g_w/len(g_rs)*100:.1f}% | "
            f"{sum(g_rs)/len(g_rs):+.3f} |"
        )

    # Recent trades table
    lines += ["", "## Recent Trades", "", "| # | Symbol | Dir | Entry | Exit | R | NetR |",
              "|---|--------|-----|-------|------|----|------|"]
    for i, t in enumerate(sorted(closed, key=lambda x: x["closed_at"], reverse=True)[:20], start=1):
        netr = t.get("net_r_score")
        lines.append(
            f"| {i} | {t['symbol']} | {t['direction']} | {t['entry']:.4f} | "
            f"{t.get('exit_price'):.4f} | {t['r_multiple']:+.2f} | "
            f"{f'{netr:+.3f}' if netr is not None else '—'} |"
        )
    lines.append(
        "\n> Research only. Not part of the live forward-test sample "
        "(filter source='live' for the official n≥50 count)."
    )

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    logger.info("Wrote %s", path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="crypto-backtest",
        description="Research backtest runner over frozen v2 engine (no look-ahead).",
    )
    parser.add_argument("--symbols", required=True, help="Comma-separated list, e.g. BTC,ETH,SOL")
    parser.add_argument("--interval", default="1h", choices=["15m", "1h", "4h", "1d"])
    parser.add_argument("--start", required=True, help="ISO date, e.g. 2025-01-01")
    parser.add_argument("--end", required=True, help="ISO date, e.g. 2025-12-31")
    parser.add_argument("--equity", type=float, default=10000.0, help="Initial equity")
    parser.add_argument("--risk-pct", type=float, default=1.0, help="Risk pct per trade")
    parser.add_argument("--out", default="backtest_results", help="Output directory")
    parser.add_argument("--no-db", action="store_true", help="Do not write to SQLite")
    args = parser.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    # Validate start/end format
    for d in (args.start, args.end):
        pd.Timestamp(d)

    out_dir = Path(args.out)
    run_backtest(
        symbols=symbols,
        interval=args.interval,
        start=args.start,
        end=args.end,
        equity=args.equity,
        risk_pct=args.risk_pct,
        out_dir=out_dir,
        write_db=not args.no_db,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
