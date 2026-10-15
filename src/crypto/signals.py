"""Backward-compatible re-export of the frozen v2_swing signal engine.

The real implementation now lives under:
    crypto/strategies/v2_swing/signals.py

This module forwards every public name so existing callers (backtest,
signal_agent, mcp_agent) keep working without changes. New dashboard code
should route through the strategy registry (crypto.strategies.compute_for).
"""

from crypto.strategies.v2_swing.signals import (  # noqa: F401
    ATR_SL_MULT,
    ATR_TP_MULT,
    ENGINE_ENV_VAR,
    LONG_THRESHOLD,
    PATTERN_CONF_MULTIPLIER,
    PATTERN_STRENGTH_MIN,
    SHORT_THRESHOLD,
    TREND_GATE_MIN,
    V2Signal,
    WEIGHTS,
    FactorScores,
    Signal,
    active_engine_version,
    compute_for_strategy,
    compute_signal,
    compute_signal_v1,
    compute_signal_v2,
    factor_market,
    factor_momentum,
    factor_trend,
    factor_volatility,
    factor_volume,
    signal_to_dict,
    stoch_trigger,
)
