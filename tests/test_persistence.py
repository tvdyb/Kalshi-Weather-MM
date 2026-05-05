"""Lag-1 residual correction and AR(1) running-max sampler."""
from __future__ import annotations

import numpy as np

from kweather.theo.persistence import apply_lag1, running_max_distribution


def test_apply_lag1_uses_default_rho():
    out = apply_lag1(70.0, 2.0, "high")
    assert 70.0 < out < 72.0


def test_apply_lag1_handles_none():
    assert apply_lag1(70.0, None, "low") == 70.0


def test_running_max_at_zero_minutes_returns_current():
    mu, sigma, _ = running_max_distribution(75.0, 0, base_mu=72.0, base_sigma=1.0, n_paths=10)
    assert mu == 75.0
    assert sigma == 0.0


def test_running_max_grows_with_remaining_time():
    rng = np.random.default_rng(0)
    mu_short, _, _ = running_max_distribution(70.0, 30, 70.0, 1.0, n_paths=2000, rng=rng)
    rng = np.random.default_rng(0)
    mu_long, _, _ = running_max_distribution(70.0, 300, 70.0, 1.0, n_paths=2000, rng=rng)
    assert mu_long > mu_short
