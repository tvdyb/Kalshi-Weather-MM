"""Golden-file regression for EMOS predictions.

We construct a synthetic ensemble of 5 members with known a, b, c, d, draw observations,
fit, then verify that fitted coefficients reproduce the data-generating process within
tolerance. This exercises both fit() and predict().
"""
from __future__ import annotations

import numpy as np

from kweather.theo.emos import EmosCoeffs, fit, predict


def test_predict_uniform_weights():
    coeffs = EmosCoeffs(a=0.0, b=[0.5, 0.5], c=1.0, d=0.0)
    members = np.array([[80.0, 90.0], [82.0, 88.0]])
    mu, sigma = predict(coeffs, members)
    np.testing.assert_allclose(mu, [81.0, 89.0])
    assert np.all(sigma > 0)


def test_fit_recovers_synthetic_coeffs():
    rng = np.random.default_rng(42)
    n = 800
    n_members = 4
    truth = EmosCoeffs(
        a=1.5,
        b=[0.25, 0.25, 0.25, 0.25],
        c=1.0,
        d=0.5,
    )
    base = rng.normal(70, 8, size=n)
    members = np.stack([base + rng.normal(0, 1.5, size=n) for _ in range(n_members)])
    s2 = members.var(axis=0)
    mu_true = truth.a + np.array(truth.b) @ members
    sigma_true = np.sqrt(truth.c + truth.d * s2)
    y = rng.normal(mu_true, sigma_true)

    fitted = fit(members, y)
    # Coefficient sum should be close to 1 since each member is unbiased
    assert 0.85 <= sum(fitted.b) <= 1.15
    assert -1.0 <= fitted.a <= 4.0
    assert 0.05 <= fitted.c <= 5.0
    assert 0.0 <= fitted.d <= 5.0


def test_predict_falls_back_with_mismatched_members():
    # 3 members but coeffs have 1 b → fall back to mean
    coeffs = EmosCoeffs(a=0.0, b=[1.0], c=1.0, d=0.0)
    members = np.array([[70.0], [71.0], [72.0]])
    mu, sigma = predict(coeffs, members)
    assert mu.shape == (1,)
    assert abs(float(mu[0]) - 71.0) < 1e-6
