"""Lag-1 residual persistence correction.

If yesterday at this station the realized Tmax/Tmin came in at observed_y, and our
forecast was mu_yest, the residual is r1 = observed_y - mu_yest. We add a fraction
of r1 (the persistence weight rho) to today's forecast mean. rho is fit per-station
and per-target as the AR(1) coefficient of the residual series; if no fit is
available we use rho = 0.30 for high, 0.20 for low.

This module also exposes a Monte-Carlo sampler for the running-max conditional
distribution used by the intraday hot path. We model temperature evolution from
the latest observation forward to the daily peak as an OU/AR(1) process with
per-minute innovation sigma scaled by HRRR's residual std at this lead.
"""
from __future__ import annotations

import numpy as np

DEFAULT_RHO = {"high": 0.30, "low": 0.20}


def apply_lag1(
    mu: float,
    yesterday_residual: float | None,
    target: str,
    rho: float | None = None,
) -> float:
    if yesterday_residual is None:
        return mu
    r = rho if rho is not None else DEFAULT_RHO.get(target, 0.25)
    return mu + r * yesterday_residual


def running_max_distribution(
    current_temp: float,
    minutes_remaining: int,
    base_mu: float,
    base_sigma: float,
    rho: float = 0.85,
    per_minute_sigma: float = 0.20,
    n_paths: int = 5000,
    rng: np.random.Generator | None = None,
) -> tuple[float, float, np.ndarray]:
    """Monte-Carlo running-max for the rest of the day.

    Returns (running_max_mu, running_max_sigma, sample_array). The temperature
    evolves as an AR(1) toward base_mu with reversion strength implied by rho
    over the remaining minutes; innovations have std per_minute_sigma.

    The running max is taken over the entire path including the current_temp
    observation, since the realized peak so far is already known to the caller.
    """
    rng = rng or np.random.default_rng()
    if minutes_remaining <= 0:
        arr = np.full(n_paths, current_temp)
        return float(current_temp), 0.0, arr

    n = minutes_remaining
    eps = rng.normal(scale=per_minute_sigma, size=(n_paths, n))
    # Mean-reverting process: x_{t+1} = base_mu + rho*(x_t - base_mu) + eps
    # Vectorized iteration is unavoidable due to the recursion, but n is small (≤ 720).
    paths = np.empty((n_paths, n + 1))
    paths[:, 0] = current_temp
    rho_minute = rho ** (1.0 / max(n, 1))
    for t in range(n):
        paths[:, t + 1] = base_mu + rho_minute * (paths[:, t] - base_mu) + eps[:, t]
    rmax = paths.max(axis=1)
    return float(rmax.mean()), float(rmax.std(ddof=1)), rmax


def running_min_distribution(
    current_temp: float,
    minutes_remaining: int,
    base_mu: float,
    base_sigma: float,
    rho: float = 0.85,
    per_minute_sigma: float = 0.20,
    n_paths: int = 5000,
    rng: np.random.Generator | None = None,
) -> tuple[float, float, np.ndarray]:
    mu, sigma, paths = running_max_distribution(
        -current_temp,
        minutes_remaining,
        -base_mu,
        base_sigma,
        rho,
        per_minute_sigma,
        n_paths,
        rng,
    )
    return -mu, sigma, -paths
