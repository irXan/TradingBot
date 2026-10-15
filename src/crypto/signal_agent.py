"""Crypto market signal agent (agno + DeepSeek).

The agent exposes deterministic market-data/indicator/signal tools and uses
DeepSeek (deepseek-v4-flash) to orchestrate analysis and synthesize a report
with direction, confidence, and SL/TP per asset.

Run:
    crypto-signals                          # analyze top-10, daily candles
    crypto-signals --limit 5 --interval 4h
    crypto-signals --message "Focus on BTC and ETH"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

from crypto.indicators import compute_all
from crypto.market_data import get_ohlcv, get_top_coins
from crypto.signals import compute_signal, signal_to_dict

# Load DEEPSEEK_API_KEY from .env when present
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# rich/agno print the markdown report to the console; force UTF-8 so unicode
# characters (arrows, em-dashes, …) render on Windows cp1252 terminals.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DEEPSEEK_MODEL_ID = os.getenv("DEEPSEEK_MODEL_ID", "deepseek-v4-flash")

INTERVAL_PERIOD = {"1d": "1y", "4h": "3mo", "1h": "1mo"}


@lru_cache(maxsize=64)
def _cached_ohlcv(symbol: str, interval: str) -> object:
    """Cached OHLCV so repeated tool calls don't refetch."""
    period = INTERVAL_PERIOD.get(interval, "1y")
    return get_ohlcv(symbol, interval=interval, period=period)


# ---------------------------------------------------------------------------
# Tools (agno auto-converts functions with docstrings into agent tools)
# ---------------------------------------------------------------------------


def get_market_overview(limit: int = 10) -> str:
    """Fetch the top `limit` coins by market cap with live price, market cap,
    24h volume, and 24h/7d price changes. Returns JSON."""
    try:
        coins = get_top_coins(limit=limit)
        return json.dumps(coins, indent=2, default=str)
    except Exception as exc:  # pragma: no cover - defensive for LLM
        return json.dumps({"error": str(exc)})


def get_technical_indicators(symbol: str, interval: str = "1d") -> str:
    """Compute the latest technical indicators (EMA20/50/200, RSI14, MACD,
    ATR, Bollinger %B, volume ratio) for a symbol from OHLCV candles.
    Returns JSON."""
    try:
        df = _cached_ohlcv(symbol.upper(), interval)
        ind = compute_all(df)
        return json.dumps(ind, indent=2, default=str)
    except Exception as exc:  # pragma: no cover - defensive for LLM
        return json.dumps({"error": f"{symbol}: {exc}"})


def analyze_signal(symbol: str, interval: str = "1d") -> str:
    """Compute the full composite signal for one symbol: direction
    (LONG/SHORT/NEUTRAL), confidence (0-100), and ATR-based stop-loss /
    take-profit levels. Returns JSON."""
    try:
        market = {c["symbol"]: c for c in get_top_coins(limit=25)}.get(symbol.upper(), {})
        df = _cached_ohlcv(symbol.upper(), interval)
        ind = compute_all(df)
        sig = compute_signal(symbol.upper(), ind, market=market)
        payload = signal_to_dict(sig)
        payload["indicators"] = ind
        return json.dumps(payload, indent=2, default=str)
    except Exception as exc:  # pragma: no cover - defensive for LLM
        return json.dumps({"error": f"{symbol}: {exc}"})


def analyze_universe(limit: int = 10, interval: str = "1d") -> str:
    """Analyze the top `limit` coins by market cap and return every composite
    signal in one JSON payload. Use this for a full market scan."""
    try:
        coins = get_top_coins(limit=limit)
        signals = []
        for coin in coins:
            symbol = coin["symbol"]
            try:
                df = _cached_ohlcv(symbol, interval)
                ind = compute_all(df)
                sig = compute_signal(symbol, ind, market=coin)
                payload = signal_to_dict(sig)
                payload["indicators"] = ind
                signals.append(payload)
            except Exception as exc:
                signals.append({"symbol": symbol, "error": str(exc)})
        return json.dumps({"interval": interval, "signals": signals}, indent=2, default=str)
    except Exception as exc:  # pragma: no cover - defensive for LLM
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


def build_agent() -> object:
    """Build the DeepSeek-powered market signal agent."""
    from agno.agent import Agent
    from agno.models.deepseek import DeepSeek

    model = DeepSeek(id=DEEPSEEK_MODEL_ID)
    return Agent(
        name="crypto-signals",
        model=model,
        tools=[get_market_overview, get_technical_indicators, analyze_signal, analyze_universe],
        instructions=(
            "You are a senior crypto market analyst. Analyze the current crypto market "
            "using the provided tools and generate actionable trading signals.\n"
            "Workflow:\n"
            "1. Call analyze_universe (or get_market_overview) to see the top coins.\n"
            "2. For coins of interest, inspect analyze_signal / get_technical_indicators "
            "details (RSI, MACD, EMA alignment, ATR, volume).\n"
            "3. Produce a markdown report covering: market regime (risk-on/off), "
            "per-asset signal table (direction, confidence, entry, SL, TP), the top 2-3 "
            "setups with rationale, and risk warnings.\n"
            "Always show the numbers you used (RSI, EMA spread, ATR) and cite them. "
            "Be explicit that signals are informational, not financial advice."
        ),
        markdown=True,
        add_datetime_to_context=True,
    )


async def _run(limit: int, interval: str, message: str | None) -> None:
    agent = build_agent()
    prompt = message or (
        f"Analyze the top {limit} cryptocurrencies on {interval} candles and generate "
        "a full market signal report: regime, per-asset direction + confidence + "
        "stop-loss/take-profit, top setups, and risks."
    )
    await agent.aprint_response(prompt, stream=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto market signal agent (agno + DeepSeek)")
    parser.add_argument("--limit", type=int, default=10, help="Top N coins by market cap (default 10)")
    parser.add_argument("--interval", choices=sorted(INTERVAL_PERIOD), default="1d", help="Candle interval (default 1d)")
    parser.add_argument("--message", help="Custom prompt for the agent (overrides default scan)")
    args = parser.parse_args()

    if not os.getenv("DEEPSEEK_API_KEY"):
        raise SystemExit(
            "DEEPSEEK_API_KEY not set. Create a .env file with DEEPSEEK_API_KEY=sk-... "
            "or set the environment variable."
        )

    asyncio.run(_run(args.limit, args.interval, args.message))


if __name__ == "__main__":
    main()
