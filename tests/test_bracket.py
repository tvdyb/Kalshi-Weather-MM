"""Tests for bracket probability with the +/- 0.5 F CLI rounding adjustment."""
from __future__ import annotations

from kweather.theo.bracket import bracket_probabilities, bracket_probability, fair_price_cents
from kweather.types import Bracket


def test_partition_sums_to_one():
    # A complete partition: open below 70, ranges, open above 80.
    brackets = [
        Bracket(lo=None, hi=70, label="B69"),
        Bracket(lo=70, hi=73, label="70-T-72"),
        Bracket(lo=73, hi=76, label="73-T-75"),
        Bracket(lo=76, hi=79, label="76-T-78"),
        Bracket(lo=79, hi=82, label="79-T-81"),
        Bracket(lo=82, hi=None, label="A81"),
    ]
    probs = bracket_probabilities(brackets, mu=75.0, sigma=2.0)
    assert abs(sum(probs) - 1.0) < 1e-9


def test_centered_bracket_dominates_wings():
    brackets = [
        Bracket(lo=None, hi=70, label="B"),
        Bracket(lo=70, hi=72, label="L"),
        Bracket(lo=72, hi=74, label="C"),
        Bracket(lo=74, hi=76, label="R"),
        Bracket(lo=76, hi=None, label="A"),
    ]
    probs = bracket_probabilities(brackets, mu=73.0, sigma=1.0)
    # Center should be the largest
    assert probs[2] == max(probs)


def test_single_temp_bracket_rounding():
    # T76 → continuous interval [75.5, 76.5)
    b = Bracket(lo=76, hi=77, label="T76")
    p = bracket_probability(b, mu=76.0, sigma=1.0)
    # Should be ~0.3829 for [75.5, 76.5) under N(76,1)
    assert 0.36 < p < 0.40


def test_fair_price_cents_clamped():
    assert fair_price_cents(0.0) == 1
    assert fair_price_cents(1.0) == 99
    assert fair_price_cents(0.50) == 50
