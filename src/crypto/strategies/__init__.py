"""Strategy registry — parallel signal engines on one dashboard.

Each strategy is a self-contained package exposing a `compute_for_strategy`
entry point that returns a signal object with a uniform shape:

    direction | confidence | score | entry | stop_loss | take_profit
    factors | risk_metadata

The service layer routes by name; v2_swing logic is untouched and v3_scalp
runs beside it, both tagged by `strategy` so their forward-test samples stay
isolated in a single database.
"""

from __future__ import annotations

from typing import Any

from . import v2_swing, v3_scalp

_REGISTRY = {
    "v2_swing": v2_swing,
    "v3_scalp": v3_scalp,
}


def known_strategies() -> tuple[str, ...]:
    return tuple(_REGISTRY)


def is_known(name: str) -> bool:
    return name in _REGISTRY


def compute_for(strategy: str, symbol: str, ind: dict, df: Any, market: dict | None = None) -> Any:
    """Compute one signal for `strategy`. Raises KeyError for unknown names."""
    module = _REGISTRY[strategy]
    return module.compute_for_strategy(symbol, ind, df, market)


def signal_fields(sig: Any) -> dict[str, Any]:
    """Normalize a strategy signal into the flat row the service layer stores."""
    return {
        "direction": sig.direction,
        "confidence": sig.confidence,
        "score": sig.score,
        "entry": sig.entry,
        "stop_loss": sig.stop_loss if sig.stop_loss is not None else sig.entry,
        "take_profit": sig.take_profit if sig.take_profit is not None else sig.entry,
        "factors": sig.factors,
        "risk_metadata": getattr(sig, "risk_metadata", {}) or {},
    }
