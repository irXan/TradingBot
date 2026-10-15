"""Market data layer: CoinGecko (top coins) + Binance (OHLCV primary) + yfinance (fallback).

CoinGecko free API needs no key for the /coins/markets endpoint used here.
Binance public REST API (/api/v3/klines) provides sub-second OHLCV with no key.
yfinance is kept as a fallback only for coins not on Binance.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutTimeout
from typing import Any

import httpx
import pandas as pd

logger = logging.getLogger("crypto.market")

COINGECKO_URL = os.getenv("COINGECKO_URL", "https://api.coingecko.com/api/v3")
BINANCE_URL = "https://api.binance.com/api/v3"
USER_AGENT = {"User-Agent": "crypto-signals/0.2"}

# Failsafe: even if a socket ignores httpx's timeout (native stall on Windows),
# the worker thread that issued the request is hard-bounded so it can never
# block a scan loop / the event loop indefinitely.
_HTTP_WALL_CLOCK_GRACE = 5.0      # extra seconds beyond httpx timeout before we abort
_http_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="crypto-http")


def _http_get(url: str, *, params: dict | None = None, timeout: float) -> httpx.Response:
    """Hard-bounded synchronous GET.

    Runs the real httpx call in a dedicated sub-thread and waits at most
    ``timeout + _HTTP_WALL_CLOCK_GRACE`` wall-clock seconds. If a native
    network stall ignores httpx's own timeout, this still returns/raises so the
    caller (and the broader scan) can never wedge. Logs duration and any
    exception class so stall sources are visible in dashboard-err.log.
    """
    started = time.monotonic()
    fut = _http_executor.submit(httpx.get, url, params=params,
                                headers=USER_AGENT, timeout=timeout)
    budget = timeout + _HTTP_WALL_CLOCK_GRACE
    try:
        resp = fut.result(timeout=budget)
        logger.debug("http ok %.3fs %s", time.monotonic() - started, url)
        return resp
    except _FutTimeout:
        logger.warning(
            "http WALL-CLOCK TIMEOUT %.3fs (httpx=%s) url=%s", budget, timeout, url
        )
        raise TimeoutError(f"wall-clock timeout after {budget:.1f}s for {url}")
    except Exception as exc:  # noqa: BLE001 - surface every failure class
        logger.warning("http error %s after %.3fs url=%s",
                       type(exc).__name__, time.monotonic() - started, url)
        raise

# Binance kline interval mapping (CoinGecko/yfinance format → Binance format)
_INTERVAL_MAP = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}

# Period → max candle limit (Binance returns up to 1000 per request)
_PERIOD_LIMIT = {"1y": 365, "3mo": 90, "1mo": 30, "3d": 1000, "1w": 1000}


def get_top_coins(limit: int = 10) -> list[dict[str, Any]]:
    """Return the top `limit` coins by market cap from CoinGecko."""
    resp = _http_get(
        f"{COINGECKO_URL}/coins/markets",
        params={
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": limit,
            "page": 1,
            "sparkline": "false",
        },
        timeout=30,
    )
    resp.raise_for_status()
    rows: list[dict[str, Any]] = []
    for coin in resp.json():
        rows.append(
            {
                "symbol": coin["symbol"].upper(),
                "name": coin["name"],
                "price_usd": coin["current_price"],
                "market_cap": coin["market_cap"],
                "volume_24h": coin["total_volume"],
                "change_24h": coin["price_change_percentage_24h"],
                "change_7d": coin.get("price_change_percentage_7d_in_currency"),
            }
        )
    return rows


def get_ohlcv(symbol: str, interval: str = "1d", period: str = "1y") -> pd.DataFrame:
    """Return OHLCV DataFrame from Binance public API (< 1s per coin).
    Raises ValueError if the pair is not listed on Binance."""
    return _get_ohlcv_binance(symbol, interval, period)


# Backtest interval support: CoinGecko format → Binance format
_BT_INTERVAL_MAP = {
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}
_BT_LIMIT = 1000  # Binance max klines per request


def get_ohlcv_range(
    symbol: str,
    interval: str = "1h",
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """Fetch OHLCV across an arbitrary historical date range using Binance
    klines pagination (1000 bars/request). Supports intraday intervals
    (15m, 1h, 4h, 1d) not covered by the period-limited get_ohlcv.

    start/end are ISO date strings (e.g. '2025-01-01'). Inclusive of the
    window. Returns a DataFrame indexed by UTC Datetime with OHLCV columns.

    Raises ValueError if the pair is not listed on Binance.
    """
    bn_interval = _BT_INTERVAL_MAP.get(interval, "1d")
    pair = f"{symbol.upper()}USDT"

    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000) if start else None
    end_ms = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000) if end else None

    frames: list[pd.DataFrame] = []
    cursor = start_ms
    while True:
        params = {"symbol": pair, "interval": bn_interval, "limit": _BT_LIMIT}
        if cursor is not None:
            params["startTime"] = cursor
        if end_ms is not None:
            params["endTime"] = end_ms
        resp = _http_get(f"{BINANCE_URL}/klines", params=params, timeout=15)
        if resp.status_code == 400:
            raise ValueError(f"No OHLCV data for {symbol} (pair {pair} not listed on Binance)")
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        frames.append(_kline_frame(data))
        # Paginate: advance past the last returned open_time.
        last_ts = int(data[-1][0])
        if cursor is not None and last_ts <= cursor or len(data) < _BT_LIMIT:
            break
        cursor = last_ts + 1

    if not frames:
        raise ValueError(f"No OHLCV data for {symbol} in requested range")
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    if end_ms is not None:
        df = df[df.index <= pd.Timestamp(end, tz="UTC")]
    return df


def _kline_frame(data: list) -> pd.DataFrame:
    """Convert raw Binance kline rows into a standard OHLCV DataFrame."""
    df = pd.DataFrame(data, columns=[
        "open_time", "Open", "High", "Low", "Close", "Volume",
        "close_time", "quote_vol", "trades", "taker_buy_vol",
        "taker_buy_quote_vol", "ignore",
    ])
    df["Date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.set_index("Date")
    for col in ("Open", "High", "Low", "Close", "Volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df[["Open", "High", "Low", "Close", "Volume"]].dropna()


def _get_ohlcv_binance(symbol: str, interval: str, period: str) -> pd.DataFrame:
    """Fetch OHLCV from Binance public klines endpoint. No API key required.
    Returns empty DataFrame if the pair is not listed on Binance (no yfinance fallback)."""
    pair = f"{symbol.upper()}USDT"
    bn_interval = _INTERVAL_MAP.get(interval, "1d")
    limit = _PERIOD_LIMIT.get(period, 365)

    resp = _http_get(
        f"{BINANCE_URL}/klines",
        params={"symbol": pair, "interval": bn_interval, "limit": limit},
        timeout=10,
    )
    if resp.status_code == 400:
        raise ValueError(f"No OHLCV data for {symbol} (pair {pair} not listed on Binance)")
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError(f"No OHLCV data for {symbol} (pair {pair} empty)")
    return _kline_frame(data)


def _get_ohlcv_yfinance(symbol: str, interval: str, period: str) -> pd.DataFrame:
    """Fallback: yfinance with 20s timeout."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
    import yfinance as yf

    SYMBOL_TO_YF = {"TON": "TON11419-USD"}
    ticker = SYMBOL_TO_YF.get(symbol.upper(), f"{symbol.upper()}-USD")

    def _fetch() -> pd.DataFrame:
        d = yf.download(ticker, interval=interval, period=period, progress=False, auto_adjust=True)
        if d.empty:
            raise ValueError(f"No OHLCV data for {symbol} (ticker {ticker})")
        if isinstance(d.columns, pd.MultiIndex):
            d.columns = d.columns.get_level_values(0)
        return d[["Open", "High", "Low", "Close", "Volume"]].dropna()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_fetch)
        try:
            return future.result(timeout=20)
        except FuturesTimeout:
            raise ValueError(f"OHLCV fetch timed out for {symbol} (ticker {ticker})")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"OHLCV fetch failed for {symbol}: {exc}") from exc
