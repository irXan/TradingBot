"""v2_swing strategy package — frozen composite swing engine.

Re-export the signal entry point; the implementation lives in signals.py.
"""

from .signals import compute_for_strategy, compute_signal, signal_to_dict  # noqa: F401

STRATEGY = "v2_swing"
