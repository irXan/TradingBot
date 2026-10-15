"""Dashboard service layer: periodic market scans, paper-trade lifecycle.

`scan_once` pulls the top coins, computes indicators/signals, snapshots them
into the store, and auto-resolves any open tracked trades whose stop-loss or
take-profit was touched. `open_trades` enforces the max-concurrent rule and
volatility-based position sizing (risk_quote / stop distance).
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict
from functools import lru_cache
from typing import Any

import pandas as pd

from . import config as _cfg
from . import patterns as _patterns
from . import store
from . import strategies
from .indicators import compute_all
from .market_data import get_ohlcv, get_top_coins

logger = logging.getLogger("crypto.dashboard")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

INTERVAL_PERIOD = {"1d": "1y", "4h": "3mo", "1h": "1mo", "15m": "3d", "5m": "3d"}

STABLECOINS = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDP", "USDE", "USDS"}


def _factor_dict(factors: Any) -> dict[str, Any]:
    """v1 factors are a dataclass; v2/v3 factors are already a plain dict."""
    return factors if isinstance(factors, dict) else asdict(factors)


def _scan_limit() -> int:
    return int(os.getenv("DASHBOARD_SCAN_LIMIT", "10"))


@lru_cache(maxsize=64)
def _cached_ohlcv(symbol: str, interval: str) -> pd.DataFrame:
    period = INTERVAL_PERIOD.get(interval, "1y")
    return get_ohlcv(symbol, interval=interval, period=period)


def clear_cache() -> None:
    _cached_ohlcv.cache_clear()


def _r_multiple(direction: str, entry: float, stop_loss: float, exit_price: float) -> float:
    denominator = entry - stop_loss if direction == "LONG" else stop_loss - entry
    move = exit_price - entry if direction == "LONG" else entry - exit_price
    return move / denominator


def _slip_entry(direction: str, entry: float) -> float:
    """Entry price adjusted for order-book slippage."""
    buf = _cfg.SLIPPAGE_BUFFER
    return entry * (1 + buf) if direction == "LONG" else entry * (1 - buf)


def _slip_exit(direction: str, exit_price: float) -> float:
    """Exit price adjusted for order-book slippage."""
    buf = _cfg.SLIPPAGE_BUFFER
    return exit_price * (1 - buf) if direction == "LONG" else exit_price * (1 + buf)


def _net_r(direction: str, entry: float, stop_loss: float, exit_price: float) -> float:
    """Net R-multiple after slippage and round-trip taker fees."""
    slipped_entry = _slip_entry(direction, entry)
    slipped_exit = _slip_exit(direction, exit_price)
    fee = _cfg.active_taker_fee()
    denom = slipped_entry - stop_loss if direction == "LONG" else stop_loss - slipped_entry
    if abs(denom) < 1e-15:
        return 0.0
    move = slipped_exit - slipped_entry if direction == "LONG" else slipped_entry - slipped_exit
    fee_penalty = slipped_entry * fee + slipped_exit * fee
    return (move - fee_penalty) / abs(denom)


def scan_once(strategy: str = "v2_swing") -> dict[str, Any]:
    """Run one full market scan for a strategy and resolve open trades against
    fresh prices. Interval and trade config come from the strategy config."""
    import time as _time

    cfg = _cfg.get_strategy_config(strategy)
    interval = cfg["interval"]
    target = _scan_limit()
    scan_start = _time.monotonic()
    SCAN_BUDGET = 120  # hard cap: save whatever we got after 2 min

    # Over-fetch: coins without a Yahoo ticker (new listings, tokenized assets)
    # fail OHLCV lookup, so request extra candidates to hit the target count.
    coins = get_top_coins(limit=target * 2)
    rows: list[dict[str, Any]] = []
    prices: dict[str, float] = {}
    errors: list[str] = []
    for coin in coins:
        if len(rows) >= target:
            break
        if _time.monotonic() - scan_start > SCAN_BUDGET:
            errors.append(f"scan budget exhausted after {len(rows)}/{target} coins")
            break
        symbol = coin["symbol"]
        if symbol in STABLECOINS:
            continue
        try:
            df = _cached_ohlcv(symbol, interval)
            ind = compute_all(df)
            if ind["price"] <= 0 or ind["atr"] <= 0:
                # Zero-range candles (illiquid/stale feed) produce meaningless
                # SL/TP levels; excluding them keeps the journal clean.
                errors.append(f"{symbol}: degenerate OHLCV (atr=0)")
                continue
            ind.update(_patterns.latest(df))
            sig = strategies.compute_for(strategy, symbol, ind, df, market=coin)
            fields = strategies.signal_fields(sig)
            prices[symbol] = ind["price"]
            if _cfg.strategy_observe(strategy):
                # OBSERVE mode: record the exact position that WOULD have been
                # opened (using the unchanged sizing rules) without opening it.
                ind["hypothetical__observe"] = True
                if fields["direction"] != "NEUTRAL":
                    try:
                        hypo = _spot_sizing(
                            strategy, fields["entry"], fields["stop_loss"],
                            confidence=fields.get("confidence", 0),
                        )
                        ind["hypothetical__size_quote"] = hypo["size_quote"]
                        ind["hypothetical__risk_quote"] = hypo["actual_risk_quote"]
                        ind["hypothetical__target_risk_quote"] = hypo["target_risk_quote"]
                        ind["hypothetical__actual_risk_quote"] = hypo["actual_risk_quote"]
                        ind["hypothetical__risk_utilization_pct"] = hypo["risk_utilization_pct"]
                        ind["hypothetical__remaining_cash_after_entry"] = hypo["remaining_cash_after_entry"]
                        ind["hypothetical__cash_capped"] = hypo["cash_capped"]
                        ind["hypothetical__cash_cap_reason"] = hypo["cash_cap_reason"]
                    except ValueError as exc:  # noqa: BLE001 - sizing may be blocked (cash/sizing rules)
                        ind["hypothetical__error"] = str(exc)
            rows.append(
                {
                    "symbol": symbol,
                    "name": coin.get("name"),
                    "price": ind["price"],
                    "change_24h": coin.get("change_24h"),
                    "direction": fields["direction"],
                    "confidence": fields["confidence"],
                    "score": fields["score"],
                    "entry": fields["entry"],
                    "stop_loss": fields["stop_loss"],
                    "take_profit": fields["take_profit"],
                    "factors": _factor_dict(fields["factors"]),
                    "indicators": ind,
                    "strategy": strategy,
                }
            )
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not kill the scan
            logger.warning("scan failed for %s: %s", symbol, exc)
            errors.append(f"{symbol}: {exc}")

    ts = store.save_scan(interval, rows, strategy=strategy) if rows else None
    resolved = resolve_open_trades(prices)
    return {
        "strategy": strategy,
        "interval": interval,
        "ts": ts,
        "scanned": len(rows),
        "errors": errors,
        "resolved_trades": resolved,
    }


def _spot_sizing(strategy: str, entry: float, stop_loss: float,
                 confidence: int = 0) -> dict[str, Any]:
    """Cash-only SPOT sizing for a strategy's simulated account (no leverage).

    Each strategy holds its own independent $100 cash balance. Sizing never
    assumes leverage: a position's quoted notional (size_quote) is capped by
    the strategy's own cash so it can never exceed remaining cash, and the sum
    of all OPEN positions never exceeds the strategy's cash balance.

    Allocation is conviction-weighted: higher-confidence signals may take a
    larger slice of the equity (up to alloc_ceiling_pct), lower-confidence ones
    a smaller slice (down to alloc_floor_pct). The size is still bounded by
    cash and by the risk-based notional (target_risk / stop_distance).

    Returns the computed audit values. Raises ValueError when opening would
    violate the rules (degenerate stop, or remaining cash below the
    configurable minimum).
    """
    cfg = _cfg.get_strategy_config(strategy)
    equity = cfg["capital"]                      # standing simulated equity
    target_risk = equity * cfg["risk_pct"] / 100.0
    stop_distance_pct = abs(entry - stop_loss) / entry
    if stop_distance_pct <= 0:
        raise ValueError("degenerate stop level")

    deployed = sum(t["size_quote"] for t in store.list_trades(status="OPEN", strategy=strategy))
    remaining_cash = equity - deployed
    min_cash = cfg["min_cash_to_open"]
    if remaining_cash < min_cash:
        raise ValueError(
            f"remaining cash ${remaining_cash:.2f} below min ${min_cash:.2f} to open"
        )

    risk_based_size = target_risk / stop_distance_pct
    # Conviction-weighted allocation: confidence 0..100 maps onto the
    # [alloc_floor_pct, alloc_ceiling_pct] slice of equity.
    conf = max(0, min(100, int(confidence)))
    alloc_frac = (cfg["alloc_floor_pct"] + (cfg["alloc_ceiling_pct"] - cfg["alloc_floor_pct"])
                  * conf / 100.0) / 100.0
    alloc_quote = equity * alloc_frac
    max_position_quote = min(alloc_quote, remaining_cash)
    actual_size = min(risk_based_size, max_position_quote)

    cash_capped = actual_size < risk_based_size
    reasons: list[str] = []
    if cash_capped:
        if abs(actual_size - remaining_cash) <= 1e-6:
            reasons.append("remaining_cash")
        if abs(actual_size - alloc_quote) <= 1e-6:
            reasons.append(f"conviction_alloc({alloc_frac*100:.0f}%)")
        if not reasons:
            reasons.append("cash_cap")

    actual_risk = actual_size * stop_distance_pct
    utilization = (actual_risk / target_risk * 100) if target_risk else 0.0
    remaining_after = remaining_cash - actual_size

    return {
        "size_quote": round(actual_size, 2),
        "risk_quote": round(target_risk, 2),
        "cash_capped": cash_capped,
        "cash_cap_reason": ";".join(reasons) if reasons else None,
        "target_risk_quote": round(target_risk, 2),
        "actual_risk_quote": round(actual_risk, 2),
        "risk_utilization_pct": round(utilization, 2),
        "remaining_cash_after_entry": round(remaining_after, 2),
        "alloc_frac": round(alloc_frac, 4),
    }


def open_trades(strategy: str, symbols: list[str]) -> dict[str, Any]:
    """MANUAL path: open tracked paper trades for the requested symbols from the
    latest scan of this strategy."""
    cfg = _cfg.get_strategy_config(strategy)
    interval = cfg["interval"]
    if _cfg.strategy_observe(strategy):
        raise LookupError(
            f"{strategy} is frozen: {_cfg.OBSERVE_BANNER}. "
            "Existing open trades keep resolving on their SL/TP; new trades "
            "cannot be opened while the strategy is in OBSERVE mode."
        )
    scan = store.latest_scan(interval, strategy=strategy)
    if not scan or not scan["signals"]:
        raise LookupError(f"No scan available yet for '{strategy}' ({interval}). Trigger a scan first.")

    by_symbol = {s["symbol"]: s for s in scan["signals"]}
    max_active = cfg["max_active_trades"]

    opened: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for raw in symbols:
        symbol = raw.upper()
        signal = by_symbol.get(symbol)
        if signal is None:
            skipped.append({"symbol": symbol, "reason": "not in latest scan"})
            continue
        if signal["direction"] == "NEUTRAL":
            skipped.append({"symbol": symbol, "reason": "signal is NEUTRAL"})
            continue
        if store.count_active_trades(strategy) >= max_active:
            skipped.append({"symbol": symbol, "reason": f"max {max_active} active trades"})
            break
        if _active_symbols(strategy).get(symbol):
            skipped.append({"symbol": symbol, "reason": "already open"})
            continue

        entry = signal["entry"]
        try:
            sizing = _spot_sizing(strategy, entry, signal["stop_loss"],
                                  confidence=signal.get("confidence", 0))
        except ValueError as exc:
            skipped.append({"symbol": symbol, "reason": str(exc)})
            continue

        trade = store.open_trade(
            {
                "strategy": strategy,
                "symbol": symbol,
                "interval": interval,
                "direction": signal["direction"],
                "entry": entry,
                "stop_loss": signal["stop_loss"],
                "take_profit": signal["take_profit"],
                "size_quote": sizing["size_quote"],
                "risk_quote": sizing["risk_quote"],
                **sizing,
            }
        )
        opened.append(trade)

    return {"strategy": strategy, "opened": opened, "skipped": skipped,
            "active_count": store.count_active_trades(strategy)}


def _active_symbols(strategy: str) -> dict[str, dict[str, Any]]:
    """Map symbol -> open trade for a strategy (single DB read)."""
    return {t["symbol"]: t for t in store.list_trades(status="OPEN", strategy=strategy)}


def auto_open(strategy: str) -> dict[str, Any]:
    """AUTO path: open fresh non-NEUTRAL signals up to max_active_trades for
    the strategy, skipping symbols that are already open. No manual clicks."""
    cfg = _cfg.get_strategy_config(strategy)
    interval = cfg["interval"]
    if _cfg.strategy_observe(strategy):
        return {"strategy": strategy, "opened": [], "skipped": [_cfg.OBSERVE_BANNER],
                "active_count": store.count_active_trades(strategy)}
    scan = store.latest_scan(interval, strategy=strategy)
    if not scan or not scan["signals"]:
        return {"strategy": strategy, "opened": [], "skipped": ["no scan yet"], "active_count": 0}

    max_active = cfg["max_active_trades"]
    active = _active_symbols(strategy)
    ordered = sorted(scan["signals"], key=lambda s: s["confidence"], reverse=True)

    opened: list[dict[str, Any]] = []
    skipped: list[str] = []
    for signal in ordered:
        if store.count_active_trades(strategy) >= max_active:
            skipped.append("max_active_reached")
            break
        if signal["direction"] == "NEUTRAL":
            continue
        symbol = signal["symbol"]
        if symbol in active:
            continue

        entry = signal["entry"]
        try:
            sizing = _spot_sizing(strategy, entry, signal["stop_loss"],
                                  confidence=signal.get("confidence", 0))
        except ValueError as exc:
            skipped.append(f"{symbol}: {exc}")
            continue

        try:
            trade = store.open_trade(
                {
                    "strategy": strategy,
                    "symbol": symbol,
                    "interval": interval,
                    "direction": signal["direction"],
                    "entry": entry,
                    "stop_loss": signal["stop_loss"],
                    "take_profit": signal["take_profit"],
                    "size_quote": sizing["size_quote"],
                    "risk_quote": sizing["risk_quote"],
                    **sizing,
                }
            )
            opened.append(trade)
            active[symbol] = trade
        except Exception as exc:  # noqa: BLE001
            logger.warning("auto_open failed opening %s (%s): %s", strategy, symbol, exc)

    return {"strategy": strategy, "opened": opened, "skipped": skipped,
            "active_count": store.count_active_trades(strategy)}


def close_trade(trade_id: int, price: float | None = None) -> dict[str, Any]:
    """Manually close a trade at `price`, defaulting to the latest known price."""
    trade = store.get_trade(trade_id)
    if trade is None:
        raise LookupError(f"Trade {trade_id} not found")
    if trade["status"] != "OPEN":
        raise ValueError(f"Trade {trade_id} is already {trade['status']}")

    if price is None:
        price = latest_price(trade["symbol"], trade["interval"])
    # Fallback: coin dropped out of scan cache — fetch directly from yfinance
    if price is None:
        logger.info("close_trade: %s not in scan cache, fetching OHLCV directly", trade["symbol"])
        try:
            df = _cached_ohlcv(trade["symbol"], trade["interval"])
            price = float(df["Close"].iloc[-1])
        except Exception as exc:  # noqa: BLE001
            logger.warning("close_trade: OHLCV fetch failed for %s: %s", trade["symbol"], exc)
    if price is None:
        raise ValueError(f"No live price available for {trade['symbol']}; pass an explicit exit price")

    r = _r_multiple(trade["direction"], trade["entry"], trade["stop_loss"], price)
    net_r = _net_r(trade["direction"], trade["entry"], trade["stop_loss"], price)
    logger.info("close_trade: %s #%s @ %.6f (%s) r=%.2f net_r=%.4f",
                trade["symbol"], trade_id, price, trade["direction"], r, net_r)
    return store.close_trade(trade_id, price, "MANUAL_CLOSE", r, net_r)


DATA_UNAVAILABLE_NOTE = (
    "Legacy paper trade; symbol unavailable on Binance; "
    "excluded from performance evaluation."
)


def archive_data_unavailable(trade_id: int, note: str | None = None) -> dict[str, Any]:
    """Archive an OPEN trade to a terminal DATA_UNAVAILABLE status.

    Used for legacy paper trades whose symbol has no market data (e.g. not
    listed on Binance), so they can never be resolved to an exit. The trade is
    closed with no exit price and no R-multiple: it carries no P&L and is
    excluded from every performance metric (scorecard, expectancy, PF, win
    rate, losing streak, equity curve).
    """
    trade = store.get_trade(trade_id)
    if trade is None:
        raise LookupError(f"Trade {trade_id} not found")
    if trade["status"] != "OPEN":
        raise ValueError(f"Trade {trade_id} is already {trade['status']}")
    note = note or DATA_UNAVAILABLE_NOTE
    logger.info("archive_data_unavailable: %s #%s -> DATA_UNAVAILABLE (%s)",
                trade["symbol"], trade_id, trade["strategy"])
    return store.close_trade(trade_id, None, "DATA_UNAVAILABLE", None, None, note=note)


def replay_missed_exits() -> list[dict[str, Any]]:
    """Reconcile OPEN trades against candles printed while the dashboard was off.

    Walks each open trade's OHLCV chronologically from opened_at; the first
    SL/TP touch closes the trade AT THE LEVEL (matching how a resting exchange
    order would fill). Conservative ordering: SL checked before TP within the
    same candle. This makes offline gaps retroactively accurate.
    """
    resolved: list[dict[str, Any]] = []
    for trade in store.list_trades(status="OPEN"):
        try:
            df = _cached_ohlcv(trade["symbol"], trade["interval"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("replay fetch failed for %s: %s", trade["symbol"], exc)
            continue
        opened = pd.Timestamp(trade["opened_at"])
        index = df.index if df.index.tz is not None else df.index.tz_localize("UTC")
        window = df[index > opened]
        direction, entry = trade["direction"], trade["entry"]
        sl, tp = trade["stop_loss"], trade["take_profit"]
        for _, row in window.iterrows():
            if direction == "LONG":
                hit_sl, hit_tp = row["Low"] <= sl, row["High"] >= tp
            else:
                hit_sl, hit_tp = row["High"] >= sl, row["Low"] <= tp
            if not (hit_sl or hit_tp):
                continue
            level, status = (sl, "SL_HIT") if hit_sl else (tp, "TP_HIT")
            r = _r_multiple(direction, entry, sl, level)
            net_r = _net_r(direction, entry, sl, level)
            closed = store.close_trade(trade["id"], level, status, r, net_r)
            logger.info("replay closed %s #%s at %s (%s) net_r=%.4f",
                        trade["symbol"], trade["id"], level, status, net_r)
            resolved.append(closed)
            break
    return resolved


def resolve_open_trades(prices: dict[str, float]) -> list[dict[str, Any]]:
    """Auto-close OPEN trades whose SL/TP was reached. SL checked first."""
    replay_missed_exits()
    resolved: list[dict[str, Any]] = []
    for trade in store.list_trades(status="OPEN"):
        price = prices.get(trade["symbol"])
        if price is None:
            continue
        direction = trade["direction"]
        hit_sl = price <= trade["stop_loss"] if direction == "LONG" else price >= trade["stop_loss"]
        hit_tp = price >= trade["take_profit"] if direction == "LONG" else price <= trade["take_profit"]
        status = "SL_HIT" if hit_sl else ("TP_HIT" if hit_tp else None)
        if status is None:
            continue
        r = _r_multiple(direction, trade["entry"], trade["stop_loss"], price)
        net_r = _net_r(direction, trade["entry"], trade["stop_loss"], price)
        closed = store.close_trade(trade["id"], price, status, r, net_r)
        resolved.append(closed)
    return resolved


def latest_price_map() -> dict[str, float]:
    """Merge the freshest known price per symbol across all strategies' scans."""
    out: dict[str, float] = {}
    intervals = {t["interval"] for t in store.list_trades(status="OPEN")}
    for name in _cfg.known_strategies():
        intervals.add(_cfg.strategy_interval(name))
    for interval in intervals:
        for name in _cfg.known_strategies():
            scan = store.latest_scan(interval, strategy=name)
            if not scan:
                continue
            for s in scan["signals"]:
                out[s["symbol"]] = s["price"]
    return out


def latest_price(symbol: str, interval: str | None = None) -> float | None:
    if interval:
        for name in _cfg.known_strategies():
            scan = store.latest_scan(interval, strategy=name)
            if scan:
                for s in scan["signals"]:
                    if s["symbol"] == symbol.upper():
                        return s["price"]
    return latest_price_map().get(symbol.upper())


def positions_view(strategy: str = "v2_swing") -> dict[str, Any]:
    """Active + closed LIVE trades for a strategy with live R-multiples and P&L."""
    prices = latest_price_map()
    active = []
    total_open_risk = 0.0
    deployed = 0.0
    for t in store.list_trades(status="OPEN", source="live", strategy=strategy):
        row = dict(t)
        deployed += float(t["size_quote"] or 0)
        price = prices.get(t["symbol"])
        row["live_price"] = price
        if price is not None:
            row["live_r"] = _r_multiple(t["direction"], t["entry"], t["stop_loss"], price)
            row["pnl_quote"] = round(row["live_r"] * t["risk_quote"], 2)
        actual_risk = t.get("actual_risk_quote")
        total_open_risk += float(actual_risk) if actual_risk is not None else 0.0
        active.append(row)

    cfg = _cfg.get_strategy_config(strategy)
    equity = cfg["capital"]
    remaining = equity - deployed
    alloc_ceiling = equity * cfg["alloc_ceiling_pct"] / 100.0
    cash = {
        "equity": round(equity, 2),
        "deployed_cash": round(deployed, 2),
        "remaining_cash": round(remaining, 2),
        "total_open_risk": round(total_open_risk, 2),
        "alloc_floor_pct": cfg["alloc_floor_pct"],
        "alloc_ceiling_pct": cfg["alloc_ceiling_pct"],
        "max_position_quote": round(min(alloc_ceiling, remaining), 2),
        "min_cash_to_open": cfg["min_cash_to_open"],
        "max_active_trades": cfg["max_active_trades"],
        "capital": cfg["capital"],
        "risk_pct": cfg["risk_pct"],
    }
    return {"cash": cash, "active": active,
            "closed": [t for t in store.list_trades(source="live", strategy=strategy)
                       if t["status"] != "DATA_UNAVAILABLE"]}


def reset_forward_test(strategy: str | None = None) -> dict[str, Any]:
    """Archive every existing live paper trade so the forward test restarts
    fresh.

    All live trades (open or closed) for `strategy` (or all strategies when
    None) are marked perf_excluded=1: they leave the scorecard, equity curve,
    win rate, expectancy, PF, streaks, and the cash account entirely. OPEN
    trades are closed/archived first so each strategy's cash balance returns to
    its full starting capital. Nothing is deleted — history remains queryable.
    """
    strategies = [strategy] if strategy else list(_cfg.known_strategies())
    note = "Forward-test reset: historic live trades excluded from evaluation."
    archived_active = 0
    excluded = 0
    for strat in strategies:
        for t in store.list_trades(source="live",
                                   strategy=strat,
                                   include_excluded=True):
            if t["status"] == "OPEN":
                store.close_trade(t["id"], None, "ARCHIVED_RESET", None, None,
                                  note=note)
                store.exclude_from_perf(t["id"], note)
                archived_active += 1
            else:
                store.exclude_from_perf(t["id"], note)
                excluded += 1
    return {"strategies": strategies, "archived_active": archived_active,
            "excluded": excluded,
            "note": "cash balance and forward-test metrics reset to starting capital"}


def scorecard(strategy: str = "v2_swing") -> dict[str, Any]:
    """Performance stats over CLOSED LIVE trades for a strategy only. Pass bars
    per the fund plan: win rate > 40%, expectancy > +0.30R, profit factor > 1.5,
    n >= 50. Backtest trades (source='backtest') are excluded so the official
    forward-test n>=50 count is never contaminated by research data. Legacy
    trades archived as DATA_UNAVAILABLE (no market data) are excluded from the
    count and every performance metric.
    """
    closed = [t for t in store.list_trades(source="live", strategy=strategy)
              if t["r_multiple"] is not None and t["status"] != "DATA_UNAVAILABLE"]
    closed.sort(key=lambda t: t["closed_at"] or "")
    rs = [t["r_multiple"] for t in closed]

    wins = sum(1 for r in rs if r > 0)
    losses = sum(1 for r in rs if r <= 0)
    gross_win = sum(r for r in rs if r > 0)
    gross_loss = abs(sum(r for r in rs if r <= 0))

    worst_streak = current = 0
    for r in rs:
        current = current + 1 if r <= 0 else 0
        worst_streak = max(worst_streak, current)

    equity_curve = []
    cum = 0.0
    for t in closed:
        cum += t["r_multiple"]
        equity_curve.append({"ts": t["closed_at"], "cum_r": round(cum, 4), "symbol": t["symbol"]})

    win_rate = wins / len(rs) * 100 if rs else 0.0
    expectancy = sum(rs) / len(rs) if rs else 0.0
    profit_factor = gross_win / gross_loss if gross_loss > 0 else (None if gross_win == 0 else float("inf"))

    checks = {
        "sample_size": {"value": len(rs), "bar": ">= 50", "pass": len(rs) >= 50},
        "win_rate": {"value": round(win_rate, 1), "bar": "> 40%", "pass": win_rate > 40},
        "expectancy_r": {"value": round(expectancy, 3), "bar": "> +0.30R", "pass": expectancy > 0.30},
        "profit_factor": {
            "value": round(profit_factor, 2) if profit_factor is not None else None,
            "bar": "> 1.5",
            "pass": bool(profit_factor and profit_factor != float("inf") and profit_factor > 1.5),
        },
        "worst_streak": {"value": worst_streak, "bar": "<= 6", "pass": worst_streak <= 6},
    }

    observe = None
    if _cfg.strategy_observe(strategy):
        observed = store.count_signals_since(strategy, "LONG", _cfg.V3_OBSERVE_SINCE)
        observe = {
            "mode": _cfg.OBSERVE_BANNER,
            "observed_since": _cfg.V3_OBSERVE_SINCE,
            "observed_not_opened_long": observed,
            "note": ("In OBSERVE mode every signal (direction, confidence, gate "
                     "reason, entry/SL/TP and hypothetical position size) is "
                     "still scanned and stored, but OBSERVED signals are NOT "
                     "included in win rate, expectancy, PF or streaks. Those "
                     "metrics cover only the closed baseline live trades that "
                     "were actually opened, including BTC #158."),
        }

    return {
        "n_closed": len(rs),
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 1),
        "expectancy_r": round(expectancy, 3),
        "total_r": round(sum(rs), 2),
        "profit_factor": profit_factor if profit_factor is None else round(profit_factor, 2),
        "worst_streak": worst_streak,
        "checks": checks,
        "equity_curve": equity_curve,
        "recent": [
            {
                "id": t["id"],
                "symbol": t["symbol"],
                "direction": t["direction"],
                "status": t["status"],
                "r": t["r_multiple"],
                "opened_at": t["opened_at"],
                "closed_at": t["closed_at"],
            }
            for t in reversed(closed[-20:])
        ],
        "observe": observe,
    }
