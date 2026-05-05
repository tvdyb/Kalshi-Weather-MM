"""Bracket probability under a Gaussian forecast with the +/- 0.5°F CLI rounding adjustment.

CLI Tmax/Tmin values are integer °F. A bracket like "73 to 77" actually corresponds
to {73, 74, 75, 76, 77}, i.e. the half-open interval [72.5, 77.5) on the underlying
continuous variable. A single-temp bracket "T76" is {76} → [75.5, 76.5).

We integrate the Gaussian PDF over the rounding-adjusted bracket. Bracket probabilities
across a complete partition sum to 1 by construction.
"""
from __future__ import annotations

from collections.abc import Iterable
from math import erf, sqrt

from kweather.types import Bracket


def _norm_cdf(x: float, mu: float, sigma: float) -> float:
    return 0.5 * (1.0 + erf((x - mu) / (sigma * sqrt(2))))


def _adjusted_bounds(b: Bracket) -> tuple[float, float]:
    # Convention: bracket.lo is the smallest integer °F included (inclusive); bracket.hi is
    # the smallest integer °F NOT included (exclusive). For a bracket like "73 to 77",
    # callers pass lo=73, hi=78. CLI rounds to integer so the continuous-equivalent
    # interval is [lo-0.5, hi-0.5). For an open-below bracket (e.g. "below 73"),
    # lo is None. For an open-above bracket (e.g. "78+"), hi is None.
    lo = (b.lo - 0.5) if b.lo is not None else float("-inf")
    hi = (b.hi - 0.5) if b.hi is not None else float("inf")
    return lo, hi


def bracket_probability(b: Bracket, mu: float, sigma: float) -> float:
    lo, hi = _adjusted_bounds(b)
    p_lo = 0.0 if lo == float("-inf") else _norm_cdf(lo, mu, sigma)
    p_hi = 1.0 if hi == float("inf") else _norm_cdf(hi, mu, sigma)
    return max(0.0, min(1.0, p_hi - p_lo))


def bracket_probabilities(brackets: Iterable[Bracket], mu: float, sigma: float) -> list[float]:
    return [bracket_probability(b, mu, sigma) for b in brackets]


def fair_price_cents(prob: float) -> int:
    """Convert probability to integer cents in [1, 99]."""
    cents = round(prob * 100)
    return max(1, min(99, int(cents)))
