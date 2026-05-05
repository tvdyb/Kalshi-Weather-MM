"""Decision-tree tests for the quoter."""
from __future__ import annotations

from datetime import UTC, datetime

from kweather.config import Risk
from kweather.quoter.decision import decide
from kweather.types import Bracket, L2Book, L2Level, QuoteState, Theo


def _risk() -> Risk:
    return Risk(
        notional_cap_usd=500,
        position_cap_contracts=1000,
        tight_threshold_cents=2,
        stale_threshold_cents=8,
        half_spread_cents=1,
        half_spread_sigma_coef=0.0,
        fee_buffer_cents=1,
        kelly_fraction=0.25,
        cancel_debounce_ms=250,
    )


def _theo(p: int, sigma: float = 1.0) -> Theo:
    return Theo(
        market_ticker="X",
        station_code="KNYC",
        target_date="2026-05-05",
        bracket=Bracket(lo=70, hi=71, label="T70"),
        fair_price_cents=p,
        fair_prob=p / 100.0,
        mu_f=70.0,
        sigma_f=sigma,
        ts=datetime.now(tz=UTC),
    )


def _book(bid: int, ask: int) -> L2Book:
    return L2Book(
        market_ticker="X",
        yes_bids=[L2Level(price_cents=bid, qty=100)],
        yes_asks=[L2Level(price_cents=ask, qty=100)],
    )


def test_two_sided_when_theo_near_mid():
    intent = decide(_theo(50), _book(48, 52), _risk())
    assert intent.state == QuoteState.TWO_SIDED
    assert intent.bid_price_cents is not None and intent.ask_price_cents is not None
    assert intent.bid_price_cents < intent.ask_price_cents


def test_bid_only_when_theo_above_mid():
    intent = decide(_theo(55), _book(48, 52), _risk())
    assert intent.state == QuoteState.BID_ONLY
    assert intent.bid_price_cents is not None
    assert intent.ask_price_cents is None


def test_ask_only_when_theo_below_mid():
    intent = decide(_theo(45), _book(48, 52), _risk())
    assert intent.state == QuoteState.ASK_ONLY
    assert intent.ask_price_cents is not None
    assert intent.bid_price_cents is None


def test_flat_when_disagreement_too_wide():
    intent = decide(_theo(70), _book(48, 52), _risk())
    assert intent.state == QuoteState.WIDE_DISAGREEMENT
    assert intent.bid_price_cents is None and intent.ask_price_cents is None


def test_no_book_returns_flat():
    intent = decide(_theo(50), L2Book(market_ticker="X"), _risk())
    assert intent.state == QuoteState.FLAT
