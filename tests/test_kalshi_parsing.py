"""Kalshi market ticker parsing."""
from __future__ import annotations

from kweather.market_data.kalshi_rest import _parse_bracket, _parse_kalshi_date


def test_parse_kalshi_date():
    assert _parse_kalshi_date("25NOV05") == "2025-11-05"
    assert _parse_kalshi_date("26JAN01") == "2026-01-01"


def test_parse_bracket_single_temp():
    b = _parse_bracket("T76")
    assert b.lo == 76 and b.hi == 77


def test_parse_bracket_range():
    b = _parse_bracket("73-T-77")
    assert b.lo == 73 and b.hi == 78


def test_parse_bracket_below():
    b = _parse_bracket("B72")
    assert b.lo is None and b.hi == 72


def test_parse_bracket_above():
    b = _parse_bracket("A77")
    assert b.lo == 78 and b.hi is None
