"""Execution configuration: mode toggle, fee schedule, slippage buffer.

Reads from environment variables; sensible defaults for paper-trade testing
and production retail deployment. Never store secrets here.
"""

from __future__ import annotations

import os


def execution_mode() -> str:
    """SPOT | FUTURES — anything other than 'futures' defaults to SPOT."""
    raw = os.getenv("EXECUTION_MODE", "SPOT").strip().upper()
    return "FUTURES" if raw == "FUTURES" else "SPOT"


# --- fee schedule (taker) ---------------------------------------------------

SPOT_TAKER_FEE: float = float(os.getenv("SPOT_TAKER_FEE", "0.0010"))      # 0.10%
FUTURES_TAKER_FEE: float = float(os.getenv("FUTURES_TAKER_FEE", "0.0004"))  # 0.04%
SLIPPAGE_BUFFER: float = float(os.getenv("SLIPPAGE_BUFFER", "0.0005"))    # 0.05%


def active_taker_fee() -> float:
    """Return the taker fee for the current execution mode."""
    return FUTURES_TAKER_FEE if execution_mode() == "FUTURES" else SPOT_TAKER_FEE


# --- strategies -------------------------------------------------------------

# Each strategy runs in parallel on the same dashboard. Data is kept in one DB
# and separated by the `strategy` tag. `execution_mode` reserves the path for
# future LIVE routing; all strategies run in PAPER for the forward-test phase.
_STRATEGY_DEFAULTS = {
    "capital": 100.0,
    "risk_pct": 2.0,
    "max_active_trades": 5,
    "execution_mode": "PAPER",
    "interval": "1h",
    "min_cash_to_open": float(os.getenv("MIN_CASH_TO_OPEN", "10.0")),
    # conviction-weighted cash allocation: a signal's slice of equity is
    # interpolated between these floors based on its confidence (0-100), so a
    # high-conviction setup can take a larger cash share without ever exceeding
    # the strategy's own balance.
    "alloc_floor_pct": float(os.getenv("ALLOC_FLOOR_PCT", "10.0")),
    "alloc_ceiling_pct": float(os.getenv("ALLOC_CEILING_PCT", "40.0")),
    # per-strategy scan cadence (seconds) + auto-open toggle for the worker loop
    "scan_seconds": int(os.getenv("DASHBOARD_SCAN_SECONDS", "60")),
    "auto_open": True,
    # `observe` freezes a strategy as a PAPER baseline: the loop keeps scanning
    # and recording every signal (with hypothetical position size), but the
    # auto-open path AND the manual open API are both disabled.
    "observe": False,
}

STRATEGIES: dict[str, dict] = {
    "v2_swing": {
        "capital": 100.0,
        "risk_pct": 2.0,
        "max_active_trades": 5,
        "execution_mode": "PAPER",
        "interval": "1h",
        "auto_open": True,
    },
    "v3_scalp": {
        "capital": 100.0,
        "risk_pct": 2.0,
        "max_active_trades": 5,
        "execution_mode": "PAPER",
        "interval": "5m",
        # Frozen baseline: PAPER + OBSERVE. v3 keeps scanning 5m candles and
        # storing every signal + hypothetical position size, but no new paper
        # trades open (auto or manual) until this flag is changed.
        "auto_open": False,
        "observe": True,
    },
}

for _config in STRATEGIES.values():
    _config.setdefault("scan_seconds", _STRATEGY_DEFAULTS["scan_seconds"])
    _config.setdefault("auto_open", _STRATEGY_DEFAULTS["auto_open"])
    _config.setdefault("observe", _STRATEGY_DEFAULTS["observe"])


# v3_scalp entered OBSERVE mode at this UTC instant. Every v3 LONG signal
# timestamped at/after this is an observed-but-not-opened signal, counted and
# displayed by the scorecard but NEVER folded into win rate/expectancy/PF.
V3_OBSERVE_SINCE = "2026-09-04T18:21:00+00:00"

OBSERVE_BANNER = "OBSERVE — NEW TRADES DISABLED"


def known_strategies() -> tuple[str, ...]:
    return tuple(STRATEGIES)


def get_strategy_config(name: str) -> dict:
    """Return the merged config for one strategy (defaults filled in)."""
    merged = dict(_STRATEGY_DEFAULTS)
    merged.update(STRATEGIES.get(name, {}))
    return merged


def strategy_interval(name: str) -> str:
    return get_strategy_config(name)["interval"]


def strategy_execution_mode(name: str) -> str:
    return get_strategy_config(name)["execution_mode"].strip().upper()


def strategy_auto_open(name: str) -> bool:
    """Operational auto-open toggle for a strategy's worker loop."""
    return bool(get_strategy_config(name)["auto_open"])


def strategy_observe(name: str) -> bool:
    """True when a strategy is frozen OBSERVE-only (signals + hypothetical
    sizing recorded, no new paper trades via auto or manual paths)."""
    return bool(get_strategy_config(name).get("observe"))
