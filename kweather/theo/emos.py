"""Ensemble Model Output Statistics (EMOS) for daily Tmax / Tmin.

Reference: Gneiting et al. (2005), "Calibrated probabilistic forecasting using ensemble
model output statistics and minimum CRPS estimation." We fit a non-homogeneous Gaussian
regression of the form:

    Y | x ~ Normal(a + b' * x_mean, c + d * S^2)

where x_mean is the multi-model ensemble mean and S^2 is the ensemble spread (sample variance
across the member forecasts). Coefficients (a, b, c, d) are fit per (station, target=high|low,
season) by minimizing CRPS over an expanding window. We use closed-form CRPS for Gaussian:

    CRPS(N(mu,sigma), y) = sigma * (z * (2*Phi(z) - 1) + 2*phi(z) - 1/sqrt(pi))

with z = (y - mu) / sigma. Minimization is via L-BFGS-B with parameter constraints b_i in
[0, 5], c >= 0.05, d >= 0.

Coefficients are persisted as a JSON dict to the cache dir. Refit nightly.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize
from scipy.special import erf

SQRT_PI = np.sqrt(np.pi)
SQRT_2 = np.sqrt(2.0)


def _phi(z: np.ndarray) -> np.ndarray:
    return np.exp(-0.5 * z * z) / np.sqrt(2 * np.pi)


def _Phi(z: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + erf(z / SQRT_2))


def _crps_gaussian(mu: np.ndarray, sigma: np.ndarray, y: np.ndarray) -> np.ndarray:
    sigma = np.maximum(sigma, 1e-6)
    z = (y - mu) / sigma
    return sigma * (z * (2 * _Phi(z) - 1) + 2 * _phi(z) - 1.0 / SQRT_PI)


@dataclass
class EmosCoeffs:
    a: float = 0.0
    b: list[float] = field(default_factory=lambda: [1.0])
    c: float = 1.0
    d: float = 0.5

    def to_dict(self) -> dict:
        return {"a": self.a, "b": list(self.b), "c": self.c, "d": self.d}

    @classmethod
    def from_dict(cls, data: dict) -> EmosCoeffs:
        return cls(a=data["a"], b=list(data["b"]), c=data["c"], d=data["d"])


def predict(
    coeffs: EmosCoeffs,
    member_means: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Apply EMOS to a stack of ensemble members.

    member_means: shape (n_members, n_obs). Each row is one model's mean forecast.
    Returns (mu, sigma) arrays of shape (n_obs,).
    """
    member_means = np.atleast_2d(member_means)
    if member_means.shape[0] != len(coeffs.b):
        # Fall back: mean across members with a single weight
        x = member_means.mean(axis=0, keepdims=True)
        b = np.array([coeffs.b[0]])
    else:
        x = member_means
        b = np.array(coeffs.b)
    mu = coeffs.a + (b[:, None] * x).sum(axis=0)
    s2 = member_means.var(axis=0, ddof=0)
    sigma = np.sqrt(np.maximum(coeffs.c + coeffs.d * s2, 1e-4))
    return mu.astype(float), sigma.astype(float)


def fit(
    member_means: np.ndarray,    # (n_members, n_obs)
    y: np.ndarray,               # (n_obs,)
    init: EmosCoeffs | None = None,
) -> EmosCoeffs:
    """Fit coefficients by minimizing mean CRPS via L-BFGS-B."""
    member_means = np.atleast_2d(member_means)
    n_members = member_means.shape[0]
    if init is None:
        init = EmosCoeffs(a=0.0, b=[1.0 / n_members] * n_members, c=1.0, d=0.5)
    s2 = member_means.var(axis=0, ddof=0)

    def unpack(theta: Sequence[float]) -> tuple[float, np.ndarray, float, float]:
        a = theta[0]
        b = np.array(theta[1 : 1 + n_members])
        c = theta[1 + n_members]
        d = theta[2 + n_members]
        return a, b, c, d

    def neg_mean_crps(theta: Sequence[float]) -> float:
        a, b, c, d = unpack(theta)
        mu = a + (b[:, None] * member_means).sum(axis=0)
        sigma = np.sqrt(np.maximum(c + d * s2, 1e-4))
        return float(np.mean(_crps_gaussian(mu, sigma, y)))

    bounds = [(-10.0, 10.0)] + [(0.0, 5.0)] * n_members + [(0.05, 50.0), (0.0, 50.0)]
    theta0 = [init.a] + list(init.b) + [init.c, init.d]
    res = minimize(neg_mean_crps, theta0, method="L-BFGS-B", bounds=bounds)
    a, b, c, d = unpack(res.x)
    return EmosCoeffs(a=float(a), b=list(map(float, b)), c=float(c), d=float(d))
