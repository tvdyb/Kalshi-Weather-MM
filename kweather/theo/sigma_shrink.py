"""Sigma shrinkage. Defaults from the backtest sigma sweep."""
from __future__ import annotations

DEFAULTS = {
    "high": 0.90,
    "low": 1.00,
}


def shrink(sigma_f: float, target: str, override: float | None = None) -> float:
    factor = override if override is not None else DEFAULTS.get(target, 1.0)
    return max(sigma_f * factor, 0.25)
